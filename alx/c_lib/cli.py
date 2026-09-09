# SPDX-License-Identifier: MIT
"""The device under test through its serial CLI: framed reads, a wire log and command helpers.

The Auralix C Library CLI takes one command line and answers with one JSON document. A response is
framed by its braces: ``read_json`` stops when they balance (braces inside strings ignored) and the
closing CR LF has arrived, no fixed waits, pretty and compact responses alike. Bytes before the
first brace are trace output the device shares on the same UART (boot banner, ``[INF]`` lines); they
are logged as ``RX(trace)``, kept in ``trace_rx`` for ``alx.c_lib.trace`` to parse, and never enter
a frame. Bytes after the frame (pipelined responses) are held back for the next read.

Every byte in both directions goes to the wire log with a timestamp relative to construction, and
``note`` adds free-text lines, so the log reads as the transcript of the session. The serial port
has one owner: while a ``Cli`` session runs, nothing else may open that port.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from alx.errors import CliError


class Wire(Protocol):
    """What ``Cli`` needs from the transport; a pyserial ``Serial`` with a short timeout fits."""

    def read(self, n: int, /) -> bytes:
        """Return up to ``n`` bytes, ``b""`` at the read timeout."""
        ...

    def write(self, data: bytes, /) -> int | None:
        """Send ``data``."""
        ...

    def flush(self) -> None:
        """Wait until the sent bytes left the port."""
        ...

    def reset_input_buffer(self) -> None:
        """Drop every received byte."""
        ...


class Cli:
    """One serial CLI session over an open pyserial port (read timeout ~0.05 s; readers poll)."""

    def __init__(self, ser: Wire, log_path: str | Path):
        self.ser = ser
        self._t0 = time.monotonic()
        # the wire log lives as long as the session; close() closes it
        self._log = Path(log_path).open("a", encoding="utf-8")  # noqa: SIM115
        self.latencies_ms: list[float] = []  # round-trip times of framed commands (for the report)
        self._pending = b""  # bytes received beyond the last frame (pipelined responses)
        self.identity: dict[
            str, str
        ] = {}  # filled by the session owner (e.g. the parsed boot banner)
        self.trace_rx = bytearray()  # trace bytes read_json skipped; take_trace() hands them over

    # -- raw wire ---------------------------------------------------------------------
    def _logline(self, direction: str, data: bytes) -> None:
        self._log.write(f"{time.monotonic() - self._t0:9.3f} {direction} {data!r}\n")
        self._log.flush()

    def note(self, text: str) -> None:
        """Write a free-text line into the wire log."""
        self._log.write(f"{time.monotonic() - self._t0:9.3f} -- {text}\n")
        self._log.flush()

    def flush_rx(self) -> None:
        """Drop everything received so far, held-back bytes included, and log it as ``RX(drop)``."""
        junk = self._pending + self.ser.read(4096)
        self._pending = b""
        if junk:
            self._logline("RX(drop)", junk)
        self.ser.reset_input_buffer()

    def _read_raw(self, n: int) -> bytes:
        """Read up to ``n`` bytes, serving bytes held back by a previous framed read first."""
        if self._pending:
            out, self._pending = self._pending[:n], self._pending[n:]
            return out
        return self.ser.read(n)

    def send(self, data: bytes) -> None:
        """Write bytes to the device and log them as ``TX``."""
        self._logline("TX", data)
        self.ser.write(data)
        self.ser.flush()

    def read_until_quiet(self, total_s: float = 2.0, quiet_s: float = 0.3) -> bytes:
        """Collect RX until it stays quiet for ``quiet_s`` or ``total_s`` expires.

        Used where the expected response is nothing or is not one JSON document.
        """
        deadline = time.monotonic() + total_s
        buf = b""
        last_rx = time.monotonic()
        while time.monotonic() < deadline:
            chunk = self._read_raw(512)
            if chunk:
                buf += chunk
                last_rx = time.monotonic()
            elif (time.monotonic() - last_rx) > quiet_s:
                break
        if buf:
            self._logline("RX", buf)
        return buf

    def read_json(self, total_s: float = 2.0) -> bytes:
        """Return exactly one JSON document as raw bytes, ``b""`` on timeout.

        The frame ends when the braces balance (braces inside strings ignored) and its CR LF
        arrived; bytes after that are held back for the next read, bytes before the first brace are
        logged as trace.
        """
        deadline = time.monotonic() + total_s
        buf = b""
        depth = 0
        in_str = False
        esc = False
        started = False
        done = False
        pre = b""
        while not done and time.monotonic() < deadline:
            chunk = self._read_raw(512)
            if not chunk:
                continue
            for i, b in enumerate(chunk):
                if not started and b != 0x7B:  # 0x7B = "{": everything before the document is trace
                    pre += bytes([b])
                    continue
                buf += bytes([b])
                c = chr(b)
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                    started = True
                elif c == "}":
                    depth -= 1
                if started and depth == 0 and buf.endswith(b"\r\n"):
                    self._pending = chunk[i + 1 :] + self._pending
                    done = True
                    break
        if pre:
            self._logline("RX(trace)", pre)
            self.trace_rx += pre
        if buf:
            self._logline("RX", buf)
        return buf

    def take_trace(self) -> bytes:
        """Return the trace bytes skipped by ``read_json`` since the last call, and forget them."""
        out = bytes(self.trace_rx)
        self.trace_rx.clear()
        return out

    # -- command level ------------------------------------------------------------------
    def command(self, line: bytes, total_s: float = 2.0) -> bytes:
        """Send one line (terminator included) and return the framed JSON response as raw bytes."""
        self.flush_rx()
        t0 = time.monotonic()
        self.send(line)
        resp = self.read_json(total_s=total_s)
        if resp:
            self.latencies_ms.append((time.monotonic() - t0) * 1000.0)
        return resp

    def command_json(self, line: bytes, total_s: float = 2.0) -> dict[str, Any]:
        """Send one line and return the parsed JSON response; ``CliError`` when none came."""
        raw = self.command(line, total_s=total_s)
        if not raw:
            raise CliError(f"no response to {line!r}")
        data: dict[str, Any] = json.loads(raw.replace(b"\r\n", b"").decode("ascii"))
        return data

    def expect_silence(self, line: bytes, quiet_s: float = 0.3) -> bytes:
        """Send a line that must produce no response; return whatever came (``b""`` is good)."""
        self.flush_rx()
        self.send(line)
        return self.read_until_quiet(total_s=quiet_s + 0.5, quiet_s=quiet_s)

    # -- the c-lib CLI vocabulary ---------------------------------------------------------
    @staticmethod
    def _ascii(text: str | bytes) -> bytes:
        return text.encode("ascii") if isinstance(text, str) else text

    def _cmd(self, name: str, term: bytes = b"\r") -> dict[str, Any]:
        return self.command_json(name.encode("ascii") + term)

    def help(self) -> dict[str, Any]:
        """Send ``help`` and return the parsed response (the command list, always pretty)."""
        return self._cmd("help")

    def reset(self) -> dict[str, Any]:
        """Send ``reset`` and return its response; the device then reboots (banner follows)."""
        return self._cmd("reset")

    def id(self) -> dict[str, Any]:
        """Send ``id`` and return the parsed response."""
        return self._cmd("id")

    def get(self) -> dict[str, Any]:
        """Send ``get`` (every item) and return the parsed response."""
        return self._cmd("get")

    def get_param(self) -> dict[str, Any]:
        """Send ``get-param`` and return the parsed response (``status`` + ``data``)."""
        return self._cmd("get-param")

    def get_var(self) -> dict[str, Any]:
        """Send ``get-var`` and return the parsed response."""
        return self._cmd("get-var")

    def get_flag(self) -> dict[str, Any]:
        """Send ``get-flag`` and return the parsed response."""
        return self._cmd("get-flag")

    def get_const(self) -> dict[str, Any]:
        """Send ``get-const`` and return the parsed response."""
        return self._cmd("get-const")

    def get_trig(self) -> dict[str, Any]:
        """Send ``get-trig`` and return the parsed response."""
        return self._cmd("get-trig")

    def set_param(self, key: str | bytes, val: str | bytes, term: bytes = b"\r") -> dict[str, Any]:
        """Send ``set-param --key <key> --val <val>`` and return the parsed response."""
        line = b"set-param --key " + self._ascii(key) + b" --val " + self._ascii(val) + term
        return self.command_json(line)

    def get_params(self) -> dict[str, Any]:
        """Send ``get-param`` and return only its ``data`` object (the shortcut tests use most)."""
        data: dict[str, Any] = self.get_param()["data"]
        return data

    def sync(self) -> None:
        """Sync both ends: a bare terminator flushes the device line buffer, then RX is drained."""
        self.send(b"\r")
        self.read_until_quiet(total_s=0.6, quiet_s=0.15)
        self.flush_rx()

    def close(self) -> None:
        """Close the wire log (the serial port stays the caller's)."""
        self._log.close()
