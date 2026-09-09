# SPDX-License-Identifier: MIT
"""Live watch: read and write firmware variables by name while the core runs, through the probe.

The IDE feature of the same name (IAR, SEGGER Embedded Studio, VisualGDB), headless: the ELF of the
flashed build turns a variable name into an address and a type (``arm-none-eabi-gdb``, offline, once
per image), the probe reads or writes the bytes without halting the core. The automotive form of the
same idea is ASAM MCD: measurement and calibration of ECU variables over XCP with an A2L
description; here the probe is the transport and the ELF is the description.

Table, owned by the product::

    VARIABLES = {
        "input_ok": ("app.inputs.ok", "bool"),      # a C expression, resolved from the ELF
        "out_pct": ("app.out.pct", "u8"),
        "gpio_in": (0x41004420, "u32"),             # an absolute address: a peripheral register
    }
    watch = LiveWatch(probe, elf, VARIABLES)
    watch.snapshot()                    -> {"input_ok": True, "out_pct": 105, "gpio_in": 4096}
    watch.write("out_pct", 50)          -> 50, injected and read back

Formats: u8 i8 bool u16 i16 u32 i32 f32 (little-endian Cortex-M). Reads are one probe session per
snapshot (about a second with J-Link Commander) and sequential, not atomic; polling can miss a
transient. What comes back is the firmware's intent, not a physical measurement. Peripheral
registers must be side-effect-free to read. CPU core registers need a halted core and are not part
of the watch. The ELF must belong to the flashed image: a mismatch reads the wrong address without
any error.
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


def encode(value, fmt: str) -> bytes:
    """Encode ``value`` as ``fmt`` (see FORMATS); raises ValueError when it does not fit."""
    code, _ = FORMATS[fmt]
    if fmt == "bool":
        return struct.pack(code, 1 if value else 0)
    try:
        return struct.pack(code, value)
    except struct.error as ex:
        raise ValueError(f"{value!r} does not fit {fmt}: {ex}") from ex


def _is_address(expr) -> bool:
    return isinstance(expr, int) or (
        isinstance(expr, str) and expr.strip().lower().startswith("0x")
    )


class LiveWatch:
    """Named firmware variables of one ELF, read and written through a probe while the core runs."""

    def __init__(self, probe, elf: str | Path, variables: dict, gdb: str | Path | None = None):
        """Bind ``variables`` (``{name: (expression | address, format)}``) of ``elf`` to ``probe``.

        ``probe`` is anything with ``read_mem`` and ``write_mem`` (an ``alx.debug_probe`` adapter).
        gdb is only needed when at least one entry is a C expression.
        """
        self.probe = probe
        self.elf = Path(elf)
        self.variables = dict(variables)
        for name, (_, fmt) in self.variables.items():
            if fmt not in FORMATS:
                raise ValueError(f"{name}: unknown format {fmt!r} (known: {', '.join(FORMATS)})")
        if not self.elf.exists():
            raise ProbeError(f"ELF of the flashed build not found: {self.elf}")
        self.addr: dict[str, int] = {}
        symbolic = []
        for name, (expr, _) in self.variables.items():
            if _is_address(expr):
                self.addr[name] = int(expr, 16) if isinstance(expr, str) else int(expr)
            else:
                symbolic.append((name, expr))
        if symbolic:
            gdb_path = Path(gdb) if gdb else find_gdb()
            if gdb_path is None:
                raise ProbeError(
                    "arm-none-eabi-gdb not found (needed to resolve variable names from the ELF)"
                )
            addrs = resolve_addresses(gdb_path, self.elf, [expr for _, expr in symbolic])
            self.addr.update(dict(zip([n for n, _ in symbolic], addrs, strict=True)))

    def _entry(self, name: str) -> tuple[int, str]:
        if name not in self.variables:
            raise KeyError(f"{name!r} is not in the watch table ({', '.join(self.variables)})")
        return self.addr[name], self.variables[name][1]

    def snapshot(self) -> dict:
        """Read every variable once in one probe session and return ``{name: value}``."""
        reads = [(self.addr[name], FORMATS[fmt][1]) for name, (_, fmt) in self.variables.items()]
        raw = self.probe.read_mem(reads)
        return {
            name: decode(raw[self.addr[name]], fmt) for name, (_, fmt) in self.variables.items()
        }

    def read(self, name: str):
        """Read one variable (one probe session)."""
        addr, fmt = self._entry(name)
        return decode(self.probe.read_mem([(addr, FORMATS[fmt][1])])[addr], fmt)

    def write(self, name: str, value):
        """Inject ``value`` into one variable while the core runs; return the value read back.

        The probe verifies the write by reading the bytes back (``ProbeError`` on a mismatch). The
        firmware may overwrite the variable again at any time; the test decides when that matters.
        """
        addr, fmt = self._entry(name)
        data = encode(value, fmt)
        result = self.probe.write_mem(addr, data)
        return decode(result.read_back[addr], fmt)
