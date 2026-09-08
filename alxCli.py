#*******************************************************************************
# @file			alxCli.py
# @brief		Auralix Python Library - CLI Module
# @copyright	Copyright (C) Auralix d.o.o. All rights reserved.
#*******************************************************************************

"""The device under test seen through its serial CLI (the Auralix C Library CLI: one command line in,
one JSON document out) - framed reads, a wire log and the command helpers a bench suite needs.

Framing: a response is ONE JSON document. read_json() stops when the braces balance (braces inside
strings ignored) and the closing CR LF arrived - no fixed waits, pretty and compact responses alike.
Bytes BEFORE the first brace are not part of the frame: the device shares this UART with its trace
output (boot banner, [INF] lines), so they are logged as RX(trace) and skipped. Bytes AFTER the frame
(pipelined responses) are held back for the next read.

Wire log: every byte in both directions goes to log_path with a timestamp relative to construction;
note() adds a free-text line, so the log reads as the transcript of the session.
"""

import json
import time
from pathlib import Path


class Cli:
    """One serial CLI session. `ser` is an open pyserial port (read timeout ~0.05 s recommended:
    the framed readers poll)."""

    def __init__(self, ser, log_path: Path):
        self.ser = ser
        self._t0 = time.monotonic()
        self._log = open(log_path, "a", encoding="utf-8")
        self.latencies_ms = []      # round-trip times of framed commands (for the report)
        self._pending = b""         # bytes received beyond the last frame (pipelined responses)
        self.identity = {}          # filled by the session owner (e.g. the parsed boot banner)

    # -- raw wire ---------------------------------------------------------
    def _logline(self, direction: str, data: bytes):
        self._log.write(f"{time.monotonic() - self._t0:9.3f} {direction} {data!r}\n")
        self._log.flush()

    def note(self, text: str):
        self._log.write(f"{time.monotonic() - self._t0:9.3f} -- {text}\n")
        self._log.flush()

    def flush_rx(self):
        junk = self._pending + self.ser.read(4096)
        self._pending = b""
        if junk:
            self._logline("RX(drop)", junk)
        self.ser.reset_input_buffer()

    def _read_raw(self, n: int) -> bytes:
        """Serial read that first serves bytes held back by a previous framed read."""
        if self._pending:
            out, self._pending = self._pending[:n], self._pending[n:]
            return out
        return self.ser.read(n)

    def send(self, data: bytes):
        self._logline("TX", data)
        self.ser.write(data)
        self.ser.flush()

    def read_until_quiet(self, total_s: float = 2.0, quiet_s: float = 0.3) -> bytes:
        """Collect RX until it stays quiet for quiet_s (or total_s expires).
        Used where the EXPECTED response is nothing or is not one JSON document."""
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
        """Framed read: EXACTLY one JSON document = stop when the braces balance
        (braces inside strings ignored) and its CRLF arrived; bytes after that
        (pipelined responses) are held back for the next read. Returns raw bytes
        (b"" on timeout). Works for pretty and compact responses alike.
        Bytes BEFORE the first brace are trace, not frame: logged as RX(trace) and skipped,
        so a trace line between two responses cannot corrupt the JSON framing."""
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
                if not started and b != 0x7B:      # 0x7B = "{": everything before the document is trace, not frame
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
                    self._pending = chunk[i + 1:] + self._pending
                    done = True
                    break
        if pre:
            self._logline("RX(trace)", pre)
        if buf:
            self._logline("RX", buf)
        return buf

    # -- command level ---------------------------------------------------
    def command(self, line: bytes, total_s: float = 2.0) -> bytes:
        """Send one line (terminator INCLUDED by the caller), return the framed JSON response (raw)."""
        self.flush_rx()
        t0 = time.monotonic()
        self.send(line)
        resp = self.read_json(total_s=total_s)
        if resp:
            self.latencies_ms.append((time.monotonic() - t0) * 1000.0)
        return resp

    def command_json(self, line: bytes, total_s: float = 2.0) -> dict:
        raw = self.command(line, total_s=total_s)
        assert raw, f"no response to {line!r}"
        return json.loads(raw.replace(b"\r\n", b"").decode("ascii"))

    def expect_silence(self, line: bytes, quiet_s: float = 0.3) -> bytes:
        """Send a line that must produce NO response; returns whatever came (b"" = good)."""
        self.flush_rx()
        self.send(line)
        return self.read_until_quiet(total_s=quiet_s + 0.5, quiet_s=quiet_s)

    def set_param(self, key: bytes, val: bytes, term: bytes = b"\r") -> dict:
        return self.command_json(b"set-param --key " + key + b" --val " + val + term)

    def get_params(self) -> dict:
        return self.command_json(b"get-param\r")["data"]

    def sync(self):
        """Device-side line-buffer sync + host RX drain (both ends of the wire): a bare terminator
        flushes any unterminated bytes left in the device's line buffer as one (possibly invalid) command."""
        self.send(b"\r")
        self.read_until_quiet(total_s=0.6, quiet_s=0.15)
        self.flush_rx()

    def close(self):
        self._log.close()
