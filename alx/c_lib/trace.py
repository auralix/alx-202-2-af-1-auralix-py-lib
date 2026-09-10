# SPDX-License-Identifier: MIT
"""Parsers for the Auralix C Library trace output: the boot banner, the identity and reset blocks.

The firmware writes trace lines on its debug UART unrequested, ``[<timestamp>] [<LEVEL>] <text>``,
and right after a reset it prints who it is. On a shared UART these lines arrive between CLI
responses: ``alx.c_lib.cli.Cli`` keeps them out of the JSON frames and offers them in ``trace_rx``;
a trace-only UART delivers them through ``alx.serial_logger``. This module only parses bytes, it
does not know where they came from.

Two identity shapes, because the library has two
------------------------------------------------
``parse_banner`` reads a product's own one-line-per-field banner (``FW Started:`` / ``- FW Name:`` /
``- FW Version:`` / ``- FW Bin:``), which a product writes itself.

``parse_id_trace`` reads ``AlxId_Trace``, the library's own identity block, which every product on a
recent library prints without writing any code for it::

    AlxId_Trace - START
    FW:
    - artf: EXAMPLE-1-2-3
    - name: ExampleFw
    - ver: 1.2.3.2601020304.<40 hex>
    - bin: 2601020304_EXAMPLE-1-2-3_ExampleFw_V1-2-3_abcdef1.bin
    ...
    FW - Bootloader:
    - artf: ...

A device behind a bootloader emits the block twice, once from the bootloader and once from the
application, so the parser keeps the LAST block: after a reset that is the application, which is
what a test suite is asking about. A bootloader section inside that block is returned under
``boot_`` keys, so one call answers both "what is running" and "what launched it".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BANNER_RE = re.compile(
    rb"FW Started:.*?- FW Name: (?P<name>\S+).*?- FW Version: (?P<ver>\S+)"
    rb".*?- FW Bin: (?P<bin>\S+\.bin)",
    re.DOTALL,
)
LINE_RE = re.compile(rb"^\[(?P<ts>[^\]]*)\] \[(?P<level>[A-Z]{3})\] ?(?P<text>.*?)\r?$")

ID_START = "AlxId_Trace - START"
ID_SECTIONS = {"FW:": "", "FW - Bootloader:": "boot_"}
ID_FIELDS = ("artf", "name", "ver", "bin")
ID_FIELD_RE = re.compile(r"^- (?P<key>[a-z_]+): (?P<val>.*)$")

RST_START = "AlxRst_Trace - START"
RST_FLAGS = {
    "Software": "sw",
    "nRST Pin": "rst_pin",
    "Window Watchdog (WWDG)": "wwdg",
    "Independent Watchdog (IWDG)": "iwdg",
    "Low-power Management": "low_power_mgmt",
    "Power-on (POR) / Brown-out (BOR)": "por_or_bor",
    "Power-on (POR)": "por",
    "Firewall": "firewall",
    "Option Byte Loader": "option_byte_loader",
}
RST_FLAG_RE = re.compile(r"^- (?P<label>.+): (?P<val>[01])$")


@dataclass(frozen=True)
class TraceLine:
    """One trace line: the firmware's timestamp text, the level (INF, WRN, ERR) and the text."""

    timestamp: str
    level: str
    text: str


def parse_banner(raw: bytes) -> dict[str, str]:
    """Extract name, version, bin and 7-char build hash from a boot transcript, ``{}`` if none.

    The firmware traces ``- FW Name: X``, ``- FW Version: <maj.min.patch.date.fullhash>`` and
    ``- FW Bin: <date>_..._V<maj>-<min>-<patch>_<hash7>.bin`` right after reset.
    """
    match = BANNER_RE.search(raw)
    if not match:
        return {}
    ident: dict[str, str] = {k: v.decode("ascii", "replace") for k, v in match.groupdict().items()}
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


def parse_id_trace(raw: bytes) -> dict[str, str]:
    """Extract the last ``AlxId_Trace`` identity block, ``{}`` when there is none.

    Returns ``artf``, ``name``, ``ver`` and ``bin`` of the firmware that printed it, plus ``hash7``
    taken from the bin file name, and the same four under ``boot_`` when the block carries a
    bootloader section. Fields outside the two identity sections (compiler and library versions,
    the pcb and bom identities) are ignored: they are inventory, not the answer to "which image".
    """
    ident: dict[str, str] = {}
    prefix: str | None = None
    for line in parse_lines(raw):
        text = line.text.strip()
        if text.startswith(ID_START):
            ident, prefix = {}, None
            continue
        if text in ID_SECTIONS:
            prefix = ID_SECTIONS[text]
            continue
        if prefix is None:
            continue
        match = ID_FIELD_RE.match(text)
        if match is None:
            prefix = None  # any other line ends the section
            continue
        if match["key"] in ID_FIELDS:
            ident[prefix + match["key"]] = match["val"]
    for key in ("bin", "boot_bin"):
        if key in ident:
            ident[key.replace("bin", "hash7")] = ident[key].rsplit("_", 1)[-1].removesuffix(".bin")
    return ident


def parse_rst_traces(raw: bytes) -> list[dict[str, bool]]:
    """Extract every ``AlxRst_Trace`` reset-reason block, oldest first, ``[]`` when there is none.

    The library prints one block per boot, listing the MCU's reset flags as they were when
    ``AlxRst_Init`` read them::

        AlxRst_Trace - START - STM32 Reset Reason:
        - Software: 0
        - nRST Pin: 1
        - Power-on (POR): 0

    Every block in ``raw`` is returned, because a device behind a bootloader prints two per
    reset and the interesting question is usually how they differ. Keys are the C structure's
    field names in snake case (``sw``, ``rst_pin``, ``wwdg``, ``iwdg``, ``low_power_mgmt``,
    ``por_or_bor``, ``por``, ``firewall``, ``option_byte_loader``); a flag whose label this
    module does not know is kept under the label itself rather than dropped, so a new MCU
    family stays visible.

    A block ends at the first line that is not ``- <label>: <0 or 1>``, which is what separates it
    from the identity block that follows.

    One caveat belongs with the numbers rather than with any product using them: on STM32 the
    library's read of the flags CLEARS them, so only the first code to call ``AlxRst_Init`` after a
    reset can see a reason. Anything that runs later - an application behind a bootloader, most of
    all - reads a block of zeros no matter what caused the reset.
    """
    blocks: list[dict[str, bool]] = []
    inside = False
    for line in parse_lines(raw):
        text = line.text.strip()
        if text.startswith(RST_START):
            blocks.append({})
            inside = True
            continue
        if not inside:
            continue
        match = RST_FLAG_RE.match(text)
        if match is None:
            inside = False
            continue
        blocks[-1][RST_FLAGS.get(match["label"], match["label"])] = match["val"] == "1"
    return blocks
