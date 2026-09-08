# SPDX-License-Identifier: MIT
"""Live view into a running firmware: named variables read from RAM through the debug probe.

Addresses are resolved from the ELF of the flashed build with ``arm-none-eabi-gdb`` (offline, no
target). Reading the variables the main loop writes is non-intrusive when the probe reads memory
while the core runs (``DebugProbe.mem_while_running``). Cost: one probe session per snapshot (~1 s),
so everything is read in one snapshot. The variable table (``name -> (C expression, format)``)
belongs to the product; this module only resolves and decodes it. What comes back is the firmware's
intent (the value it computed), not a physical measurement, and the reads are sequential, not an
atomic snapshot.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from collections.abc import Iterable
from pathlib import Path

from alx.errors import ProbeError

# format -> (struct code, size); little-endian Cortex-M
FORMATS = {
    "u8": ("<B", 1),
    "i8": ("<b", 1),
    "bool": ("<B", 1),
    "u16": ("<H", 2),
    "i16": ("<h", 2),
    "u32": ("<I", 4),
    "i32": ("<i", 4),
    "f32": ("<f", 4),
}
ENV_GDB = "ALX_HIL_GDB"
GDB_CANDIDATES = (
    Path("C:/SysGCC/arm-eabi/bin/arm-none-eabi-gdb.exe"),
    Path("C:/Program Files (x86)/Sysprogs/VisualGDB/Keil/arm-none-eabi-gdb.exe"),
)


def find_gdb(env_var: str = ENV_GDB) -> Path | None:
    """Return arm-none-eabi-gdb from the environment variable, PATH or known toolchain paths."""
    env = os.environ.get(env_var)
    if env and Path(env).exists():
        return Path(env)
    which = shutil.which("arm-none-eabi-gdb")
    if which:
        return Path(which)
    return next((g for g in GDB_CANDIDATES if g.exists()), None)


def resolve_addresses(gdb: str | Path, elf: str | Path, exprs: Iterable[str]) -> list[int]:
    """Return the address of every C expression in the ELF, in order (one ``gdb --batch`` run)."""
    exprs = list(exprs)
    cmds: list[str] = []
    for expr in exprs:
        cmds += ["-ex", f"print/x &{expr}"]
    result = subprocess.run(
        [str(gdb), "--batch", *cmds, str(elf)], capture_output=True, text=True, timeout=60
    )
    addrs = re.findall(r"^\$\d+ = (0x[0-9a-fA-F]+)", result.stdout, re.M)
    if len(addrs) != len(exprs):
        raise ProbeError(
            f"gdb resolved {len(addrs)} of {len(exprs)} symbols:\n"
            f"{result.stdout[-600:]}\n{result.stderr[-400:]}"
        )
    return [int(a, 16) for a in addrs]


def decode(raw: bytes, fmt: str, f32_digits: int = 2):
    """Decode the first bytes of ``raw`` as ``fmt`` (see FORMATS); f32 rounded to f32_digits."""
    code, size = FORMATS[fmt]
    value = struct.unpack(code, bytes(raw[:size]))[0]
    if fmt == "bool":
        return bool(value)
    if fmt == "f32":
        return round(value, f32_digits)
    return value


class RamView:
    """Named firmware variables, resolved once from the ELF, read in one probe session each."""

    def __init__(self, probe, elf: str | Path, variables: dict, gdb: str | Path | None = None):
        """Bind the ``variables`` table of ``elf`` to a probe that has ``read_mem``."""
        self.probe = probe
        self.elf = Path(elf)
        self.variables = dict(variables)
        for name, (_, fmt) in self.variables.items():
            if fmt not in FORMATS:
                raise ValueError(f"{name}: unknown format {fmt!r} (known: {', '.join(FORMATS)})")
        if not self.elf.exists():
            raise ProbeError(f"ELF of the flashed build not found: {self.elf}")
        gdb_path = Path(gdb) if gdb else find_gdb()
        if gdb_path is None:
            raise ProbeError(
                "arm-none-eabi-gdb not found (needed to resolve RAM addresses from the ELF)"
            )
        addrs = resolve_addresses(gdb_path, self.elf, [expr for expr, _ in self.variables.values()])
        self.addr = dict(zip(self.variables, addrs, strict=True))

    def snapshot(self) -> dict:
        """Read every variable once in one probe session and return ``{name: value}``."""
        reads = [(self.addr[name], FORMATS[fmt][1]) for name, (_, fmt) in self.variables.items()]
        raw = self.probe.read_mem(reads)
        return {
            name: decode(raw[self.addr[name]], fmt) for name, (_, fmt) in self.variables.items()
        }
