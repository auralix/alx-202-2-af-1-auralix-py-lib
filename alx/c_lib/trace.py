# SPDX-License-Identifier: MIT
"""Parsers for the Auralix C Library trace output: the boot banner and trace lines.

The firmware writes trace lines on its debug UART unrequested, ``[<timestamp>] [<LEVEL>] <text>``,
and right after a reset it prints the identity banner (name, version, bin file). On a shared UART
these lines arrive between CLI responses: ``alx.c_lib.cli.Cli`` keeps them out of the JSON frames
and offers them in ``trace_rx``; a trace-only UART delivers them through ``alx.serial_logger``. This
module only parses bytes, it does not know where they came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BANNER_RE = re.compile(
    rb"FW Started:.*?- FW Name: (?P<name>\S+).*?- FW Version: (?P<ver>\S+)"
    rb".*?- FW Bin: (?P<bin>\S+\.bin)",
    re.S,
)
LINE_RE = re.compile(rb"^\[(?P<ts>[^\]]*)\] \[(?P<level>[A-Z]{3})\] ?(?P<text>.*?)\r?$")


@dataclass(frozen=True)
class TraceLine:
    """One trace line: the firmware's timestamp text, the level (INF, WRN, ERR) and the text."""

    timestamp: str
    level: str
    text: str


def parse_banner(raw: bytes) -> dict:
    """Extract name, version, bin and 7-char build hash from a boot transcript, ``{}`` if none.

    The firmware traces ``- FW Name: X``, ``- FW Version: <maj.min.patch.date.fullhash>`` and
    ``- FW Bin: <date>_..._V<maj>-<min>-<patch>_<hash7>.bin`` right after reset.
    """
    match = BANNER_RE.search(raw)
    if not match:
        return {}
    ident = {k: v.decode("ascii", "replace") for k, v in match.groupdict().items()}
    ident["hash7"] = ident["bin"].rsplit("_", 1)[-1].removesuffix(".bin")
    return ident


def parse_line(line: bytes | str) -> TraceLine | None:
    """Parse one ``[timestamp] [LEVEL] text`` line; None when it is not a trace line."""
    data = line.encode("utf-8", "replace") if isinstance(line, str) else line
    match = LINE_RE.match(data.strip(b"\n"))
    if not match:
        return None
    return TraceLine(*(match.group(g).decode("utf-8", "replace") for g in ("ts", "level", "text")))


def parse_lines(raw: bytes) -> list[TraceLine]:
    """Parse every trace line in ``raw``; non-trace bytes (frames, noise) are skipped."""
    lines = [parse_line(part) for part in raw.split(b"\n")]
    return [ln for ln in lines if ln is not None]
