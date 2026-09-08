# SPDX-License-Identifier: MIT
"""OWON P4603 programmable PSU: bench instrument driver (SCPI over the USB serial port, pyserial).

The supply next to the DUT's debug UART and the debug probe: it powers the DUT or feeds one of its
inputs, so a suite can cut and restore a supply, the real power-loss test a core reset can only
imitate.

Protocol facts (measured on firmware FV:V1.9.0 at 115200 8N1, DTR/RTS low):

* commands end with CR LF, answers end with CR LF; one answer per query, ~80..110 ms each
* ``*IDN?``, ``OUTP ON|OFF``, ``OUTP?`` (1|0), ``VOLT?``, ``CURR?``, ``VOLT:LIM?`` (OVP),
  ``CURR:LIM?`` (OCP), ``MEAS:VOLT?``, ``MEAS:CURR?``, ``MEAS:POW?`` work; ``SYST:ERR?`` is not
  supported (times out), never used
* a set command is silent: verify it with the matching query after a short settle
* the unit occasionally answers late or with an empty line: every exchange is retried
* a command that follows another within ~1 ms is dropped: prefer the confirmed calls
* a command executes ~80 ms after it was sent; ``OUTP OFF`` pulls the output to 0 V in well under
  100 ms

Safety: this driver has no method that changes voltage, current or the limits; the only write on the
wire is ``OUTP ON`` / ``OUTP OFF``. The output is switched on only after the caller declared the
setpoint the bench is supposed to run at (``expect_v``, ``expect_v_tol``, optionally ``ovp_max``)
and the supply reads back that setpoint; without a declared setpoint ``output_on`` refuses. The
policy values belong to the product's bench profile, the check belongs here.
"""

from __future__ import annotations

import logging
import time

import serial

from alx.errors import InstrumentError

log = logging.getLogger(__name__)

CRLF = b"\r\n"


class OwonP4603:
    """One OWON P4603 on one serial port, opened and identified in the constructor."""

    IDN_TAG = "P4603"

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        timeout_s: float = 5.0,
        tries: int = 3,
        retry_wait_s: float = 0.2,
        settle_s: float = 0.25,
        name: str = "PSU",
        expect_v: float | None = None,
        expect_v_tol: float = 0.1,
        ovp_max: float | None = None,
    ):
        self.port = port
        self.name = name  # role name in the log (one supply per role: "PSU", "INPUT", ...)
        self.tries = tries
        self.retry_wait_s = retry_wait_s
        self.settle_s = settle_s
        self.expect_v = expect_v  # None = no setpoint declared = output_on() refuses
        self.expect_v_tol = expect_v_tol
        self.ovp_max = ovp_max  # None = OVP not checked
        self.ser = serial.Serial(
            port, baud, timeout=timeout_s, write_timeout=timeout_s, dsrdtr=False, rtscts=False
        )
        self.ser.dtr = False
        self.ser.rts = False
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        try:
            self.idn = self.query("*IDN?")
        except Exception:
            self.ser.close()
            raise
        if self.IDN_TAG not in self.idn:
            self.ser.close()
            raise InstrumentError(f"{port}: not an OWON P4603 ({self.idn!r})")

    # -- low level ----------------------------------------------------------------------
    def _note(self, text: str) -> None:
        log.info("%s %s", self.name, text)

    def _exchange(self, cmd: str) -> str:
        self.ser.reset_input_buffer()
        self.ser.write(cmd.encode("ascii") + CRLF)
        line = self.ser.readline()
        return line.decode("ascii", "replace").strip()

    def query(self, cmd: str) -> str:
        """Query with retries; an empty answer (timeout) counts as a failed try."""
        for _ in range(self.tries):
            answer = self._exchange(cmd)
            if answer:
                return answer
            time.sleep(self.retry_wait_s)
        raise InstrumentError(f"{self.port}: no answer to {cmd!r} after {self.tries} tries")

    def query_float(self, cmd: str) -> float:
        """Query a numeric answer with retries on garbage."""
        for _ in range(self.tries):
            try:
                return float(self.query(cmd))
            except ValueError:
                time.sleep(self.retry_wait_s)
        raise InstrumentError(f"{self.port}: no numeric answer to {cmd!r}")

    def _set_output(self, on: bool) -> None:
        want = "1" if on else "0"
        for _ in range(self.tries):
            self.ser.reset_input_buffer()
            self.ser.write(b"OUTP " + (b"ON" if on else b"OFF") + CRLF)
            time.sleep(self.settle_s)
            if self.query("OUTP?") == want:
                return
            time.sleep(self.retry_wait_s)
        raise InstrumentError(f"{self.port}: OUTP {'ON' if on else 'OFF'} not confirmed")

    # -- readings -------------------------------------------------------------------------
    def output_is_on(self) -> bool:
        """Return the output state."""
        return self.query("OUTP?") == "1"

    def setpoint_v(self) -> float:
        """Return the voltage setpoint."""
        return self.query_float("VOLT?")

    def setpoint_i(self) -> float:
        """Return the current setpoint."""
        return self.query_float("CURR?")

    def ovp_v(self) -> float:
        """Return the over-voltage protection limit."""
        return self.query_float("VOLT:LIM?")

    def ocp_a(self) -> float:
        """Return the over-current protection limit."""
        return self.query_float("CURR:LIM?")

    def measure_v(self) -> float:
        """Return the measured output voltage."""
        return self.query_float("MEAS:VOLT?")

    def measure_i(self) -> float:
        """Return the measured output current."""
        return self.query_float("MEAS:CURR?")

    def measure_p(self) -> float:
        """Return the measured output power."""
        return self.query_float("MEAS:POW?")

    def bench_summary(self) -> str:
        """Return identity, setpoints, limits, output state and measurement in one line."""
        return (
            f"{self.idn} set {self.setpoint_v():.3f} V / {self.setpoint_i():.3f} A, "
            f"OVP {self.ovp_v():.1f} V, "
            f"OCP {self.ocp_a():.2f} A, output {'ON' if self.output_is_on() else 'OFF'}, "
            f"meas {self.measure_v():.3f} V {self.measure_i():.3f} A"
        )

    # -- the only writes: output on / off --------------------------------------------------
    def assert_setpoint(self) -> None:
        """Refuse to power the DUT unless the supply is at the declared setpoint (none: refuse)."""
        if self.expect_v is None:
            raise InstrumentError(
                f"{self.port}: no expected setpoint declared (expect_v) - not touching the output"
            )
        v = self.setpoint_v()
        ovp = self.ovp_v() if self.ovp_max is not None else None
        if abs(v - self.expect_v) > self.expect_v_tol or (ovp is not None and ovp > self.ovp_max):
            limit = "" if self.ovp_max is None else f" / OVP <= {self.ovp_max} V"
            raise InstrumentError(
                f"{self.port}: setpoint is {v} V / OVP {ovp} V, expected {self.expect_v} V{limit} "
                "- not touching the output"
            )

    def output_on(self) -> None:
        """Switch the output on after the setpoint check, confirmed by reading the state back."""
        self.assert_setpoint()
        self._set_output(True)
        self._note("output ON")

    def output_off(self) -> None:
        """Switch the output off, confirmed by reading the state back (no setpoint check needed)."""
        self._set_output(False)
        self._note("output OFF")

    def output_on_nowait(self) -> None:
        """Fire ``OUTP ON`` without the confirm round trip (setpoint still checked first).

        The DUT boots ~0.1 s after the command; confirm later with ``output_is_on`` or
        ``output_on``.
        """
        self.assert_setpoint()
        self.ser.reset_input_buffer()
        self.ser.write(b"OUTP ON" + CRLF)
        self._note("output ON (nowait)")

    def output_off_nowait(self) -> None:
        """Fire ``OUTP OFF`` without confirm, for cut-timing tests (0 V within ~0.1 s)."""
        self.ser.reset_input_buffer()
        self.ser.write(b"OUTP OFF" + CRLF)
        self._note("output OFF (nowait)")

    def power_cycle(self, off_s: float = 2.0) -> None:
        """Switch off, wait ``off_s`` (must cover output decay plus DUT hold-up), switch on."""
        self.output_off()
        time.sleep(off_s)
        self.output_on()

    def wait_output_decay(
        self, below_v: float = 1.0, timeout_s: float = 10.0, period_s: float = 0.05
    ) -> list:
        """Poll ``MEAS:VOLT?`` after output_off until below ``below_v``; return (t, v) samples."""
        t0 = time.monotonic()
        samples = []
        while True:
            v = self.measure_v()
            t = time.monotonic() - t0
            samples.append((t, v))
            if v < below_v or t > timeout_s:
                return samples
            time.sleep(period_s)

    def close(self) -> None:
        """Close the serial port; never raises."""
        try:
            self.ser.close()
        except Exception:  # noqa: BLE001 - closing a dead handle must never raise
            pass
