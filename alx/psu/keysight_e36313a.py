# SPDX-License-Identifier: MIT
"""Keysight E36313A independent outputs over one SCPI TCP connection.

One connection serves all three channels, numbered 1..3. Opening and closing
only identify/disconnect the instrument; they do not configure its outputs.
Setters and output enable require per-channel voltage/current ceilings in
``Limits``. Output disable does not require limits. Writes require native,
uncoupled operation; the driver never resets or changes the instrument's mode.
The caller owns the connection and instrument configuration exclusively.

Commands follow the E36300 Series Programming Guide, pages 17, 44, 68-74,
81 and 88. TCP port 5025 and LF framing follow the User's Guide, page 45.
https://www.keysight.com/ba/en/assets/9018-04577/programming-guides/9018-04577.pdf
https://www.keysight.com/pl/en/assets/9018-04576/user-manuals/9018-04576.pdf
"""

from __future__ import annotations

import contextlib
import logging
import math
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from alx.errors import InstrumentError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)


class Connection(Protocol):
    """Socket operations required by the driver and its offline tests."""

    def settimeout(self, value: float, /) -> None:
        """Bound the next socket operation in seconds."""
        ...

    def sendall(self, data: bytes, /) -> None:
        """Send the complete command."""
        ...

    def recv(self, size: int, /) -> bytes:
        """Receive up to size bytes; an empty result indicates disconnection."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...


@dataclass(frozen=True)
class Limits:
    """Per-channel write ceilings declared by the consuming bench, in V and A."""

    v_max: float | None = None
    i_max: float | None = None


@dataclass(frozen=True)
class Identity:
    """The four comma-separated fields returned by *IDN?."""

    raw: str
    manufacturer: str
    model: str
    serial_number: str
    firmware: str

    @classmethod
    def parse(cls, raw: str) -> Identity:
        """Parse a complete instrument identity."""
        fields = [field.strip() for field in raw.split(",")]
        if len(fields) != 4 or not all(fields):
            raise InstrumentError(f"Malformed instrument identity: {raw!r}")
        return cls(raw, fields[0], fields[1], fields[2], fields[3])


@dataclass(frozen=True)
class State:
    """Sequential readings of one channel; not a simultaneous measurement."""

    channel: int
    output_on: bool
    setpoint_v: float
    setpoint_i: float
    measure_v: float
    measure_i: float


class KeysightE36313A:
    """One E36313A over LAN, using explicit channel lists on every output command."""

    V_MAX = (6.0, 25.0, 25.0)
    I_MAX = (10.0, 2.0, 2.0)

    def __init__(
        self,
        host: str,
        port: int = 5025,
        timeout_s: float = 5.0,
        name: str = "PSU",
        limits: Mapping[int, Limits] | None = None,
        socket_factory: Callable[[tuple[str, int], float], Connection] = socket.create_connection,
    ) -> None:
        """Connect and identify without changing settings, output state or panel mode."""
        self.host = host
        self.port = port
        self.name = name
        self.timeout_s = self._number(timeout_s, "timeout_s")
        if self.timeout_s <= 0:
            raise InstrumentError("timeout_s must be positive")
        if type(port) is not int or not 1 <= port <= 65535:
            raise InstrumentError("port must be an integer in 1..65535")
        self.limits = dict(limits or {})
        for channel, limit in self.limits.items():
            self._channel(channel)
            for value in (limit.v_max, limit.i_max):
                if value is not None and self._number(value, "limit") < 0:
                    raise InstrumentError("Limits must be nonnegative")
        self._connection: Connection | None = None
        try:
            self._connection = socket_factory((host, port), self.timeout_s)
            self.idn = self._query("*IDN?")
            self.identity = Identity.parse(self.idn)
            if self.identity.model.upper() != "E36313A":
                raise InstrumentError(f"{host}: not a native E36313A ({self.idn!r})")
        except OSError as exc:
            self.close()
            raise InstrumentError(f"{host}:{port}: connection failed: {exc}") from exc
        except Exception:
            self.close()
            raise

    @staticmethod
    def _number(value: float, what: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InstrumentError(f"{what} must be a finite number")
        try:
            number = float(value)
        except OverflowError as exc:
            raise InstrumentError(f"{what} must be a finite number") from exc
        if not math.isfinite(number):
            raise InstrumentError(f"{what} must be a finite number")
        return number

    @staticmethod
    def _channel(channel: int) -> str:
        if type(channel) is not int or not 1 <= channel <= 3:
            raise InstrumentError("channel must be an integer in 1..3")
        return f"(@{channel})"

    def _socket(self) -> Connection:
        if self._connection is None:
            raise InstrumentError(f"{self.host}: connection is closed; open a new driver")
        return self._connection

    def _write(self, command: str) -> None:
        connection = self._socket()
        try:
            connection.settimeout(self.timeout_s)
            connection.sendall(command.encode("ascii") + b"\n")
        except OSError as exc:
            self.close()
            raise InstrumentError(f"{self.host}: send failed for {command!r}: {exc}") from exc

    def _query(self, command: str) -> str:
        self._write(command)
        connection = self._socket()
        deadline = time.monotonic() + self.timeout_s
        data = bytearray()
        try:
            while b"\n" not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("response deadline exceeded")
                connection.settimeout(remaining)
                chunk = connection.recv(4096)
                if not chunk:
                    raise InstrumentError(f"{self.host}: disconnected during {command!r}")
                data.extend(chunk)
                if len(data) > 4096:
                    raise InstrumentError(f"{self.host}: oversized response to {command!r}")
            line, _, extra = data.partition(b"\n")
            answer = line.decode("ascii").strip()
            if extra or not answer:
                raise InstrumentError(f"{self.host}: unexpected response framing to {command!r}")
        except (OSError, UnicodeError, InstrumentError) as exc:
            self.close()
            raise InstrumentError(f"{self.host}: query {command!r} failed: {exc}") from exc
        return answer

    def _query_float(self, command: str) -> float:
        text = self._query(command)
        try:
            value = float(text)
        except ValueError as exc:
            raise InstrumentError(
                f"{self.host}: nonnumeric response to {command!r}: {text!r}"
            ) from exc
        return self._number(value, f"response to {command!r}")

    def _independent(self) -> None:
        checks = (
            ("SYST:PERS:MOD?", "E3631XA"),
            ("OUTP:PAIR?", "OFF"),
            ("OUTP:TRAC?", "0"),
            ("OUTP:COUP:CHAN?", "NONE"),
        )
        for command, expected in checks:
            answer = self._query(command).upper()
            if answer != expected:
                raise InstrumentError(
                    f"{self.host}: independent native outputs required; "
                    f"{command} returned {answer!r}"
                )

    def _allowed(self, value: float, channel: int, *, voltage: bool) -> float:
        self._channel(channel)
        number = self._number(value, "voltage" if voltage else "current")
        limits = self.limits.get(channel, Limits())
        ceiling = limits.v_max if voltage else limits.i_max
        rating = (self.V_MAX if voltage else self.I_MAX)[channel - 1]
        if ceiling is None:
            raise InstrumentError(f"{self.host}: channel {channel} has no declared write limit")
        if not 0 <= number <= min(ceiling, rating):
            raise InstrumentError(
                f"{self.host}: channel {channel} value {number:g} "
                f"exceeds allowed 0..{min(ceiling, rating):g}"
            )
        return number

    def _set(self, command: str, value: float, channel: int, tolerance: float) -> float:
        self._independent()
        self._write(f"{command} {value:.6f}, {self._channel(channel)}")
        actual = self._query_float(f"{command}? {self._channel(channel)}")
        if abs(actual - value) > tolerance:
            raise InstrumentError(f"{self.host}: {command} {value:g} not confirmed: {actual:g}")
        self._allowed(actual, channel, voltage=command == "VOLT")
        log.info("%s CH%d %s set to %g", self.name, channel, command, actual)
        return actual

    def setpoint_v(self, channel: int = 1) -> float:
        """Return the programmed voltage in V."""
        return self._query_float(f"VOLT? {self._channel(channel)}")

    def setpoint_i(self, channel: int = 1) -> float:
        """Return the programmed current limit in A."""
        return self._query_float(f"CURR? {self._channel(channel)}")

    def measure_v(self, channel: int = 1) -> float:
        """Return the measured output voltage in V."""
        return self._query_float(f"MEAS:VOLT? {self._channel(channel)}")

    def measure_i(self, channel: int = 1) -> float:
        """Return the measured output current in A."""
        return self._query_float(f"MEAS:CURR? {self._channel(channel)}")

    def set_voltage(self, volts: float, channel: int = 1) -> float:
        """Set and verify voltage within this channel's declared and nominal limits."""
        return self._set("VOLT", self._allowed(volts, channel, voltage=True), channel, 0.002)

    def set_current(self, amps: float, channel: int = 1) -> float:
        """Set and verify current within this channel's declared and nominal limits."""
        return self._set("CURR", self._allowed(amps, channel, voltage=False), channel, 0.001)

    def output_is_on(self, channel: int = 1) -> bool:
        """Return commanded output state; configured delays or faults can affect voltage."""
        answer = self._query(f"OUTP? {self._channel(channel)}")
        if answer not in ("0", "1"):
            raise InstrumentError(f"{self.host}: invalid output state {answer!r}")
        return answer == "1"

    def overvoltage_limit(self, channel: int = 1) -> float:
        """Read the OVP threshold without changing it."""
        return self._query_float(f"VOLT:PROT? {self._channel(channel)}")

    def overcurrent_enabled(self, channel: int = 1) -> bool:
        """Read whether sustained constant-current operation trips the output off."""
        answer = self._query(f"CURR:PROT:STAT? {self._channel(channel)}")
        if answer not in ("0", "1"):
            raise InstrumentError(f"{self.host}: invalid OCP state {answer!r}")
        return answer == "1"

    def set_overcurrent_enabled(self, enabled: bool, channel: int = 1) -> None:
        """Enable or disable OCP and verify its state, preserving the configured delay."""
        if type(enabled) is not bool:
            raise InstrumentError("enabled must be a boolean")
        self._channel(channel)
        self._independent()
        self._write(f"CURR:PROT:STAT {int(enabled)}, {self._channel(channel)}")
        if self.overcurrent_enabled(channel) != enabled:
            raise InstrumentError(f"{self.host}: channel {channel} OCP command not confirmed")

    def _set_output(self, on: bool, channel: int) -> None:
        self._write(f"OUTP {int(on)}, {self._channel(channel)}")
        if self.output_is_on(channel) != on:
            raise InstrumentError(f"{self.host}: channel {channel} output command not confirmed")
        log.info("%s CH%d output %s", self.name, channel, "ON" if on else "OFF")

    def output_on(self, channel: int = 1) -> None:
        """Enable one output after checking both its voltage and current setpoints."""
        self._allowed(0.0, channel, voltage=True)
        self._allowed(0.0, channel, voltage=False)
        self._independent()
        self._allowed(self.setpoint_v(channel), channel, voltage=True)
        self._allowed(self.setpoint_i(channel), channel, voltage=False)
        self._set_output(True, channel)

    def output_off(self, channel: int = 1) -> None:
        """Disable one independent output; no bench limits are required."""
        self._channel(channel)
        self._independent()
        self._set_output(False, channel)

    def state(self, channel: int = 1) -> State:
        """Read one channel's commanded output state, setpoints and measurements."""
        return State(
            channel,
            self.output_is_on(channel),
            self.setpoint_v(channel),
            self.setpoint_i(channel),
            self.measure_v(channel),
            self.measure_i(channel),
        )

    def bench_summary(self, channel: int = 1) -> str:
        """Return identity and channel readings in one log-friendly line."""
        state = self.state(channel)
        return (
            f"{self.idn} CH{channel} set {state.setpoint_v:.3f} V / {state.setpoint_i:.3f} A, "
            f"output {'ON' if state.output_on else 'OFF'}, "
            f"meas {state.measure_v:.3f} V {state.measure_i:.6f} A"
        )

    def close(self) -> None:
        """Close the socket without switching outputs; safe to call repeatedly."""
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()
