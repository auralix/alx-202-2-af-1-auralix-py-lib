#*******************************************************************************
# @file			alxRamView.py
# @brief		Auralix Python Library - RAM View Module
# @copyright	Copyright (C) Auralix d.o.o. All rights reserved.
#*******************************************************************************

"""Live view into a running firmware: named variables read from RAM over the debug probe, addresses
resolved from the ELF of the flashed build with arm-none-eabi-gdb (offline, no target needed).

Reading the variables the main loop writes is non-intrusive: the probe reads RAM through the AHB-AP
while the core runs (no halt, no reset). Cost: one J-Link Commander invocation per snapshot (~1 s), so
everything is read in one snapshot. The variable table (name -> (C expression, format)) belongs to the
product; this module only resolves and decodes it. What comes back is the firmware's INTENT (the value
it computed), not a physical measurement, and the reads are sequential, not an atomic snapshot.
"""

import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

# format -> (struct code, size); little-endian Cortex-M
FORMATS = {
    "u8": ("<B", 1), "i8": ("<b", 1), "bool": ("<B", 1),
    "u16": ("<H", 2), "i16": ("<h", 2),
    "u32": ("<I", 4), "i32": ("<i", 4), "f32": ("<f", 4),
}
GDB_CANDIDATES = (Path("C:/SysGCC/arm-eabi/bin/arm-none-eabi-gdb.exe"),
                  Path("C:/Program Files (x86)/Sysprogs/VisualGDB/Keil/arm-none-eabi-gdb.exe"))


def find_gdb(env_var: str = "ALX_HIL_GDB"):
    """arm-none-eabi-gdb from the environment variable, PATH, or the known toolchain locations; None if absent."""
    env = os.environ.get(env_var)
    if env and Path(env).exists():
        return Path(env)
    which = shutil.which("arm-none-eabi-gdb")
    if which:
        return Path(which)
    return next((g for g in GDB_CANDIDATES if g.exists()), None)


def resolve_addresses(gdb, elf, exprs) -> list:
    """Address of every C expression in the ELF, in order (gdb --batch, print/x &expr)."""
    exprs = list(exprs)
    cmds = []
    for expr in exprs:
        cmds += ["-ex", f"print/x &{expr}"]
    r = subprocess.run([str(gdb), "--batch", *cmds, str(elf)], capture_output=True, text=True, timeout=60)
    addrs = re.findall(r"^\$\d+ = (0x[0-9a-fA-F]+)", r.stdout, re.M)
    if len(addrs) != len(exprs):
        raise RuntimeError(f"gdb resolved {len(addrs)} of {len(exprs)} symbols:\n{r.stdout[-600:]}\n{r.stderr[-400:]}")
    return [int(a, 16) for a in addrs]


def decode(raw: bytes, fmt: str, f32_digits: int = 2):
    code, size = FORMATS[fmt]
    value = struct.unpack(code, bytes(raw[:size]))[0]
    if fmt == "bool":
        return bool(value)
    if fmt == "f32":
        return round(value, f32_digits)
    return value


class RamView:
    def __init__(self, jlink, elf, variables: dict, gdb=None):
        """jlink: an alxJlink.JLink (or anything with read_mem8(reads, script_name) -> {addr: bytes});
        elf: the ELF of the FLASHED image; variables: {name: (expression, format)} with format in FORMATS."""
        self.jlink = jlink
        self.elf = Path(elf)
        self.variables = dict(variables)
        for name, (_, fmt) in self.variables.items():
            if fmt not in FORMATS:
                raise ValueError(f"{name}: unknown format {fmt!r} (known: {', '.join(FORMATS)})")
        if not self.elf.exists():
            raise RuntimeError(f"ELF of the flashed build not found: {self.elf}")
        gdb = Path(gdb) if gdb else find_gdb()
        if gdb is None:
            raise RuntimeError("arm-none-eabi-gdb not found (needed to resolve RAM addresses from the ELF)")
        addrs = resolve_addresses(gdb, self.elf, [expr for expr, _ in self.variables.values()])
        self.addr = dict(zip(self.variables, addrs))

    def snapshot(self) -> dict:
        """One Commander run: every variable read once, core NOT halted. {name: value}."""
        reads = [(self.addr[name], FORMATS[fmt][1]) for name, (_, fmt) in self.variables.items()]
        raw = self.jlink.read_mem8(reads, script_name="ram.jlink")
        return {name: decode(raw[self.addr[name]], fmt) for name, (_, fmt) in self.variables.items()}
