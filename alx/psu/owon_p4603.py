# SPDX-License-Identifier: MIT
"""OWON P4603 programmable DC power supply: the complete SCPI driver over a serial port (pyserial).

Sources
-------
* OWON SP/P Series Single Channel DC Power Supply Programming Manual, PDF dated 2018-10-17,
  https://www.owontechnology.eu/_downloads/3fe295a7161997bb5521ce0c8bd6976c
* OWON P4000 Series User Manual, Sep. 2019 edition V1.1.2,
  https://www.owontechnology.eu/_downloads/fd45f6e8a66047daba3469b9bf46e9b1
* verified on a P4603 with firmware FV:V1.9.0 at 115200 8N1 (the bench facts below)

Command set (the programming manual documents nothing else; page numbers are its)
--------------------------------------------------------------------------------
* ``identity``                        ``*IDN?``                  p. 4
* ``factory_reset()``                 ``*RST``                   p. 4, user manual 4.7.2
* ``measure_v/_i/_p()``               ``MEAS:VOLT? :CURR? :POW?``  p. 5-6
* ``output_on/_off()``                ``OUTP {0|1|ON|OFF}``      p. 6-7
* ``output_is_on()``                  ``OUTP?``                  p. 7
* ``set_voltage()`` ``setpoint_v()``  ``VOLT <v>`` ``VOLT?``     p. 8; 0..60 V, 1 mV (P4603)
* ``set_current()`` ``setpoint_i()``  ``CURR <a>`` ``CURR?``     p. 7; 0..3 A, 1 mA
* ``set_ovp()`` ``ovp_v()``           ``VOLT:LIM <v>`` ``:LIM?``  p. 8-9, over-voltage protection
* ``set_ocp()`` ``ocp_a()``           ``CURR:LIM <a>`` ``:LIM?``  p. 7-8, over-current protection

``*IDN?`` answers ``OWON,<model>,<serial>,FV:x.xx.xx``; ``*RST`` restores 5 V, 2 A and both
protection limits at maximum. Numeric parameters accept ``MIN``, ``MAX`` and ``DEF`` (manual p. 2).
Memory presets M1..M5, buzzer, display mode and the remote/local mode have no documented command
(front panel only). Serial side (user manual 4.6): 8 data bits fixed, baud 2400..115200 (factory
115200), parity none/odd/even, 1 or 2 stop bits. The manuals give no range for the protection
values; the bench unit accepts an OCP of 3.10 A with a 3 A rating, so the protection setters are
capped by the declared limits only.

Bench facts the manual does not state (measured, FV:V1.9.0)
-----------------------------------------------------------
* answers end with CR LF, one answer per query, 80..110 ms each; a set command is silent, so every
  setter reads the value back after a short settle and retries until it matches
* the unit occasionally answers late or with an empty line: every exchange is retried
* a command that follows another within ~1 ms is dropped: prefer the confirmed calls
* ``SYST:ERR?`` is not supported (times out) and is never used
* DTR and RTS must be driven low, otherwise the unit does not answer
* the manual asks for remote mode before SCPI; the unit accepts commands right after power-up
* a command executes ~80 ms after it was sent; ``OUTP OFF`` pulls the output to 0 V in well under
  100 ms; after an OVP/OCP trip the output is off and must be switched on again (user manual 4.3)

Safety model
------------
The driver can do everything the instrument can, but only what the bench declared in ``Limits``:
every write is refused unless a declared limit covers it (fail closed). A fixed-voltage bench
declares ``expect_v`` (and ``expect_ovp_max``): ``output_on`` requires that setpoint and no setter
is enabled. A sweeping bench declares write ceilings ``v_max``, ``i_max``, ``ovp_max``, ``ocp_max``
and the setters work up to them. ``factory_reset`` must be enabled explicitly because ``*RST``
raises both protection limits to the hardware maximum. The values belong to the product's bench
profile, the check belongs here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import serial

from alx.errors import InstrumentError

log = logging.getLogger(__name__)

CRLF = b"\r\n"
KEYWORDS = ("MIN", "MAX", "DEF")


@dataclass(frozen=True)
class Limits:
    """What the bench allows the driver to write; anything not covered is refused (fail closed).

    Checks for ``output_on``: ``expect_v`` (fixed bench: ``VOLT?`` must be within ``expect_v_tol``)
    and ``expect_ovp_max`` (``VOLT:LIM?`` must not exceed it). Write ceilings, each enabling one
    setter: ``v_max`` (``set_voltage``; without ``expect_v`` it also lets ``output_on`` accept any
    setpoint up to it), ``i_max`` (``set_current``), ``ovp_max`` (``set_ovp``, also checked by
    ``output_on`` when no ``expect_ovp_max`` is declared), ``ocp_max`` (``set_ocp``).
    ``factory_reset``: allow ``*RST``.
    """

    expect_v: float | None = None
    expect_v_tol: float = 0.1
    expect_ovp_max: float | None = None
    v_max: float | None = None
    i_max: float | None = None
    ovp_max: float | None = None
    ocp_max: float | None = None
    factory_reset: bool = False


@dataclass(frozen=True)
class Identity:
    """The ``*IDN?`` answer split into its fields: ``OWON,<model>,<serial>,FV:<firmware>``."""

    raw: str
    manufacturer: str
    model: str
    serial_number: str
    firmware: str

    @classmethod
    def parse(cls, raw: str) -> Identity:
        """Split the identity string; missing fields become empty strings."""
        parts = [p.strip() for p in raw.split(",")] + ["", "", "", ""]
        firmware = parts[3][3:] if parts[3].upper().startswith("FV:") else parts[3]
        return cls(raw, parts[0], parts[1], parts[2], firmware)


@dataclass(frozen=True)
class State:
    """One snapshot of the supply: eight queries, about one second."""

    output_on: bool
    setpoint_v: float
    setpoint_i: float
    ovp_v: float
    ocp_a: float
    measure_v: float
    measure_i: float
    measure_p: float


class OwonP4603:
    """One OWON P4603 on one serial port, opened and identified in the constructor."""

    IDN_TAG = "P4603"
    V_MAX = 60.0  # hardware ratings (user manual): 0..60 V, 0..3 A, 180 W, 1 mV / 1 mA
    I_MAX = 3.0
    P_MAX = 180.0
    V_RES = 0.001
    I_RES = 0.001
    FACTORY_V = 5.0  # what *RST sets (user manual 4.7.2); both limits go to the hardware maximum
    FACTORY_I = 2.0

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        parity: str = serial.PARITY_NONE,
        stopbits: int = 1,
        timeout_s: float = 5.0,
        tries: int = 3,
        retry_wait_s: float = 0.2,
        settle_s: float = 0.25,
        name: str = "PSU",
        limits: Limits | None = None,
        serial_factory=serial.Serial,
    ):
        self.port = port
        self.name = name  # role name in the log (one supply per role: "PSU", "INPUT", ...)
        self.tries = tries
        self.retry_wait_s = retry_wait_s
        self.settle_s = settle_s
        self.limits = limits or Limits()
        self.ser = serial_factory(
            port,
            baud,
            bytesize=8,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout_s,
            write_timeout=timeout_s,
            dsrdtr=False,
            rtscts=False,
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
        self.identity = Identity.parse(self.idn)
        if self.IDN_TAG not in self.idn:
            self.ser.close()
            raise InstrumentError(f"{port}: not an OWON P4603 ({self.idn!r})")

    # -- low level ----------------------------------------------------------------------
    def _note(self, text: str) -> None:
        log.info("%s %s", self.name, text)

    def _write(self, cmd: str) -> None:
        """Send a silent (set) command."""
        self.ser.reset_input_buffer()
        self.ser.write(cmd.encode("ascii") + CRLF)

    def _exchange(self, cmd: str) -> str:
        self._write(cmd)
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

    def _set_verified(self, cmd: str, value: float, query: str, tol: float) -> float:
        """Send ``cmd value``, settle, read back with ``query``; retry until it matches."""
        text = f"{value:.3f}"
        got = float("nan")
        for _ in range(self.tries):
            self._write(f"{cmd} {text}")
            time.sleep(self.settle_s)
            got = self.query_float(query)
            if abs(got - value) <= tol:
                return got
            time.sleep(self.retry_wait_s)
        raise InstrumentError(f"{self.port}: {cmd} {text} not confirmed ({query} reads {got})")

    def _set_output(self, on: bool) -> None:
        want = "1" if on else "0"
        for _ in range(self.tries):
            self._write("OUTP ON" if on else "OUTP OFF")
            time.sleep(self.settle_s)
            if self.query("OUTP?") == want:
                return
            time.sleep(self.retry_wait_s)
        raise InstrumentError(f"{self.port}: OUTP {'ON' if on else 'OFF'} not confirmed")

    def _resolve(self, value: float | str, what: str, hw_max: float, default: float) -> float:
        """Turn MIN / MAX / DEF into the number they stand for; validate the type."""
        if isinstance(value, str):
            key = value.upper()
            if key not in KEYWORDS:
                raise InstrumentError(
                    f"{self.port}: {what} {value!r} is not a number or MIN/MAX/DEF"
                )
            return {"MIN": 0.0, "MAX": hw_max, "DEF": default}[key]
        return float(value)

    def _allowed(self, value: float, limit: float | None, hw_max: float | None, what: str) -> None:
        """Refuse a write no limit covers, or one above the limit or the hardware rating."""
        if limit is None:
            raise InstrumentError(f"{self.port}: {what} refused - no limit declared in Limits")
        ceiling = limit if hw_max is None else min(limit, hw_max)
        if not 0.0 <= value <= ceiling:
            raise InstrumentError(f"{self.port}: {what} {value:g} refused - allowed 0..{ceiling:g}")

    # -- readings -------------------------------------------------------------------------
    def output_is_on(self) -> bool:
        """Return the output state (also False after an OVP/OCP trip)."""
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

    def state(self) -> State:
        """Return one snapshot of setpoints, limits, output state and measurements."""
        return State(
            output_on=self.output_is_on(),
            setpoint_v=self.setpoint_v(),
            setpoint_i=self.setpoint_i(),
            ovp_v=self.ovp_v(),
            ocp_a=self.ocp_a(),
            measure_v=self.measure_v(),
            measure_i=self.measure_i(),
            measure_p=self.measure_p(),
        )

    def bench_summary(self) -> str:
        """Return identity, setpoints, limits, output state and measurement in one line."""
        s = self.state()
        return (
            f"{self.idn} set {s.setpoint_v:.3f} V / {s.setpoint_i:.3f} A, OVP {s.ovp_v:.1f} V, "
            f"OCP {s.ocp_a:.2f} A, output {'ON' if s.output_on else 'OFF'}, "
            f"meas {s.measure_v:.3f} V {s.measure_i:.3f} A"
        )

    # -- setpoints and limits (each refused unless a declared limit covers it) -------------
    def set_voltage(self, volts: float | str) -> float:
        """Set the voltage (V, or MIN/MAX/DEF), verify with ``VOLT?``; return the read-back."""
        value = self._resolve(volts, "set_voltage", self.V_MAX, self.FACTORY_V)
        self._allowed(value, self.limits.v_max, self.V_MAX, "set_voltage")
        got = self._set_verified("VOLT", value, "VOLT?", self.V_RES)
        self._note(f"voltage set {got:.3f} V")
        return got

    def set_current(self, amps: float | str) -> float:
        """Set the current (A, or MIN/MAX/DEF), verify with ``CURR?``; return the read-back."""
        value = self._resolve(amps, "set_current", self.I_MAX, self.FACTORY_I)
        self._allowed(value, self.limits.i_max, self.I_MAX, "set_current")
        got = self._set_verified("CURR", value, "CURR?", self.I_RES)
        self._note(f"current set {got:.3f} A")
        return got

    def set_ovp(self, volts: float | str) -> float:
        """Set the over-voltage protection (V, or MIN/MAX/DEF); return the read-back."""
        value = self._resolve(volts, "set_ovp", self.V_MAX, self.V_MAX)
        self._allowed(value, self.limits.ovp_max, None, "set_ovp")
        got = self._set_verified("VOLT:LIM", value, "VOLT:LIM?", self.V_RES)
        self._note(f"OVP set {got:.3f} V")
        return got

    def set_ocp(self, amps: float | str) -> float:
        """Set the over-current protection (A, or MIN/MAX/DEF); return the read-back."""
        value = self._resolve(amps, "set_ocp", self.I_MAX, self.I_MAX)
        self._allowed(value, self.limits.ocp_max, None, "set_ocp")
        got = self._set_verified("CURR:LIM", value, "CURR:LIM?", self.I_RES)
        self._note(f"OCP set {got:.3f} A")
        return got

    def factory_reset(self) -> None:
        """Send ``*RST`` (5 V, 2 A, limits at maximum); refused unless ``Limits.factory_reset``."""
        if not self.limits.factory_reset:
            raise InstrumentError(f"{self.port}: factory_reset refused - not enabled in Limits")
        self._write("*RST")
        time.sleep(self.settle_s)
        self._note("factory reset")

    # -- output on / off ----------------------------------------------------------------------
    def assert_setpoint(self) -> None:
        """Refuse to power the DUT unless the declared limits cover the current setpoint and OVP."""
        lim = self.limits
        refuse = "not touching the output"
        v = self.setpoint_v()
        if lim.expect_v is not None:
            if abs(v - lim.expect_v) > lim.expect_v_tol:
                raise InstrumentError(
                    f"{self.port}: setpoint is {v} V, expected {lim.expect_v} V - {refuse}"
                )
        elif lim.v_max is not None:
            if v > lim.v_max:
                raise InstrumentError(
                    f"{self.port}: setpoint is {v} V, above v_max {lim.v_max} V - {refuse}"
                )
        else:
            raise InstrumentError(
                f"{self.port}: no expect_v or v_max declared in Limits - {refuse}"
            )
        ovp_ceiling, ovp_name = (
            (lim.expect_ovp_max, "expect_ovp_max")
            if lim.expect_ovp_max is not None
            else (lim.ovp_max, "ovp_max")
        )
        if ovp_ceiling is not None:
            ovp = self.ovp_v()
            if ovp > ovp_ceiling:
                raise InstrumentError(
                    f"{self.port}: OVP is {ovp} V, above {ovp_name} {ovp_ceiling} V - {refuse}"
                )

    def output_on(self) -> None:
        """Switch the output on after the setpoint check, confirmed by reading the state back."""
        self.assert_setpoint()
        self._set_output(True)
        self._note("output ON")

    def output_off(self) -> None:
        """Switch the output off, confirmed by reading the state back (no check, safe direction)."""
        self._set_output(False)
        self._note("output OFF")

    def output_on_nowait(self) -> None:
        """Fire ``OUTP ON`` without the confirm round trip (setpoint still checked first).

        The DUT boots ~0.1 s after the command; confirm later with ``output_is_on``.
        """
        self.assert_setpoint()
        self._write("OUTP ON")
        self._note("output ON (nowait)")

    def output_off_nowait(self) -> None:
        """Fire ``OUTP OFF`` without confirm, for cut-timing tests (0 V within ~0.1 s)."""
        self._write("OUTP OFF")
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
