"""The device under test through its serial CLI: framed reads, a wire log and command helpers.

The Auralix C Library CLI takes one command line and answers with one JSON document. A response is
framed by its braces: ``read_json`` stops when they balance (braces inside strings ignored) and the
closing CR LF has arrived, no fixed waits, pretty and compact responses alike. Bytes before the
first brace are trace output the device shares on the same UART (boot banner, ``[INF]`` lines); they
are logged as ``RX(trace)`` and skipped. Bytes after the frame (pipelined responses) are held back
for the next read.

Every byte in both directions goes to the wire log with a timestamp relative to construction, and
``note`` adds free-text lines, so the log reads as the transcript of the session. The serial port
has one owner: while a ``Cli`` session runs, nothing else may open that port.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Cli:
    """One serial CLI session over an open pyserial port (read timeout ~0.05 s; readers poll)."""

    def __init__(self, ser, log_path: str | Path):
        self.ser = ser
        self._t0 = time.monotonic()
        self._log = open(log_path, "a", encoding="utf-8")
        self.latencies_ms: list[float] = []  # round-trip times of framed commands (for the report)
        self._pending = b""  # bytes received beyond the last frame (pipelined responses)
        self.identity: dict = {}  # filled by the session owner (e.g. the parsed boot banner)

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
        if buf:
            self._logline("RX", buf)
        return buf

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

    def command_json(self, line: bytes, total_s: float = 2.0) -> dict:
        """Send one line and return the parsed JSON response; asserts that a response came."""
        raw = self.command(line, total_s=total_s)
        assert raw, f"no response to {line!r}"
        return json.loads(raw.replace(b"\r\n", b"").decode("ascii"))

    def expect_silence(self, line: bytes, quiet_s: float = 0.3) -> bytes:
        """Send a line that must produce no response; return whatever came (``b""`` is good)."""
        self.flush_rx()
        self.send(line)
        return self.read_until_quiet(total_s=quiet_s + 0.5, quiet_s=quiet_s)

    def set_param(self, key: bytes, val: bytes, term: bytes = b"\r") -> dict:
        """Send ``set-param --key <key> --val <val>`` and return the parsed response."""
        return self.command_json(b"set-param --key " + key + b" --val " + val + term)

    def get_params(self) -> dict:
        """Send ``get-param`` and return its ``data`` object."""
        return self.command_json(b"get-param\r")["data"]

    def sync(self) -> None:
        """Sync both ends: a bare terminator flushes the device line buffer, then RX is drained."""
        self.send(b"\r")
        self.read_until_quiet(total_s=0.6, quiet_s=0.15)
        self.flush_rx()

    def close(self) -> None:
        """Close the wire log (the serial port stays the caller's)."""
        self._log.close()
