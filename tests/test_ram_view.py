# SPDX-License-Identifier: MIT
"""alx.ram_view - the firmware RAM view over a scripted gdb and a fake probe (no toolchain, no target).

Proofs (ALX-1544):
  P50 addresses are resolved with one gdb batch (print/x &expr per variable) and a snapshot is one probe
      session, decoded per format
  P51 gdb resolving fewer symbols than asked raises with the gdb output
  P52 a missing ELF or a missing gdb raises before anything is run
  P53 an unknown format is rejected at construction
  P54 decode: little-endian integers of every width, bool from any non-zero byte, rounded f32
  P55 find_gdb: environment variable, PATH, known toolchain locations, else None
"""

import struct
import subprocess

import pytest

import alx.ram_view as ram_view
from alx.errors import ProbeError
from alx.ram_view import RamView, decode, find_gdb

VARS = {
    "input_ok": ("app.inputs.ok", "bool"),
    "out_pct": ("app.out.pct", "u8"),
    "volts": ("app.meas.v", "f32"),
}
ADDRS = {"input_ok": 0x20000010, "out_pct": 0x20000011, "volts": 0x20000014}


class FakeGdb:
    def __init__(self, addrs):
        self.calls = []
        self.addrs = addrs

    def __call__(self, argv, capture_output, text, timeout):
        self.calls.append(argv)
        out = "".join(f"${i + 1} = 0x{a:08x}\n" for i, a in enumerate(self.addrs))
        return subprocess.CompletedProcess(
            argv, 0, stdout=out, stderr="warning: no debugging symbols?\n"
        )


class FakeProbe:
    def __init__(self, memory: dict):
        self.memory = memory
        self.calls = []

    def read_mem(self, reads):
        reads = list(reads)
        self.calls.append(reads)
        return {a: self.memory[a][:n] for a, n in reads}


@pytest.fixture
def elf(tmp_path):
    f = tmp_path / "fw.elf"
    f.write_bytes(b"\x7fELF")
    return f


def test_ALX1544_P50_resolve_once_then_snapshot_in_one_probe_session(elf, tmp_path, monkeypatch):
    gdb = FakeGdb([ADDRS["input_ok"], ADDRS["out_pct"], ADDRS["volts"]])
    monkeypatch.setattr(ram_view.subprocess, "run", gdb)
    probe = FakeProbe(
        {0x20000010: b"\x01", 0x20000011: b"\x69", 0x20000014: struct.pack("<f", 11.75)}
    )
    view = RamView(probe, elf, VARS, gdb=tmp_path / "arm-none-eabi-gdb.exe")
    assert len(gdb.calls) == 1
    argv = gdb.calls[0]
    assert (
        argv[0] == str(tmp_path / "arm-none-eabi-gdb.exe")
        and argv[1] == "--batch"
        and argv[-1] == str(elf)
    )
    assert argv[2:-1] == [
        "-ex",
        "print/x &app.inputs.ok",
        "-ex",
        "print/x &app.out.pct",
        "-ex",
        "print/x &app.meas.v",
    ]
    assert view.addr == ADDRS
    assert view.snapshot() == {"input_ok": True, "out_pct": 105, "volts": 11.75}
    assert probe.calls == [[(0x20000010, 1), (0x20000011, 1), (0x20000014, 4)]]
    assert len(gdb.calls) == 1, "addresses are resolved once, not per snapshot"


def test_ALX1544_P51_partial_symbol_resolution_raises_with_the_gdb_output(
    elf, tmp_path, monkeypatch
):
    monkeypatch.setattr(ram_view.subprocess, "run", FakeGdb([0x20000010, 0x20000011]))
    with pytest.raises(ProbeError, match="gdb resolved 2 of 3 symbols") as ex:
        RamView(FakeProbe({}), elf, VARS, gdb=tmp_path / "gdb")
    assert "$2 = 0x20000011" in str(ex.value) and "no debugging symbols" in str(ex.value)
    assert isinstance(ex.value, RuntimeError), "callers catching RuntimeError keep working"


def test_ALX1544_P52_missing_elf_or_gdb_raises_before_running_anything(elf, tmp_path, monkeypatch):
    ran = FakeGdb([])
    monkeypatch.setattr(ram_view.subprocess, "run", ran)
    with pytest.raises(ProbeError, match="ELF of the flashed build not found"):
        RamView(FakeProbe({}), tmp_path / "nope.elf", VARS, gdb=tmp_path / "gdb")
    monkeypatch.setattr(ram_view, "find_gdb", lambda: None)
    with pytest.raises(ProbeError, match="arm-none-eabi-gdb not found"):
        RamView(FakeProbe({}), elf, VARS)
    assert ran.calls == []


def test_ALX1544_P53_unknown_format_is_rejected(elf, tmp_path):
    with pytest.raises(ValueError, match="bad: unknown format 'u64'"):
        RamView(FakeProbe({}), elf, {"bad": ("x", "u64")}, gdb=tmp_path / "gdb")


@pytest.mark.parametrize(
    "raw,fmt,value",
    [
        (b"\x69", "u8", 105),
        (b"\xff", "i8", -1),
        (b"\x00", "bool", False),
        (b"\x01", "bool", True),
        (b"\x02", "bool", True),
        (b"\x34\x12", "u16", 0x1234),
        (b"\xff\xff", "i16", -1),
        (b"\x78\x56\x34\x12", "u32", 0x12345678),
        (b"\xfe\xff\xff\xff", "i32", -2),
        (struct.pack("<f", 12.345), "f32", 12.35),
        (struct.pack("<f", 0.0), "f32", 0.0),
    ],
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_ALX1544_P54_decode_little_endian_formats(raw, fmt, value):
    assert decode(raw, fmt) == value
    assert decode(raw + b"\xaa\xaa\xaa\xaa", fmt) == value, (
        "extra bytes after the value are ignored"
    )


def test_ALX1544_P55_find_gdb_order_env_path_known_locations(tmp_path, monkeypatch):
    gdb = tmp_path / "gdb.exe"
    gdb.write_bytes(b"")
    monkeypatch.setenv("ALX_HIL_GDB", str(gdb))
    assert find_gdb() == gdb
    monkeypatch.delenv("ALX_HIL_GDB")
    monkeypatch.setattr(ram_view.shutil, "which", lambda name: str(tmp_path / "onpath" / name))
    assert find_gdb() == tmp_path / "onpath" / "arm-none-eabi-gdb"
    monkeypatch.setattr(ram_view.shutil, "which", lambda name: None)
    monkeypatch.setattr(ram_view, "GDB_CANDIDATES", (tmp_path / "missing" / "gdb.exe", gdb))
    assert find_gdb() == gdb
    monkeypatch.setattr(ram_view, "GDB_CANDIDATES", (tmp_path / "missing" / "gdb.exe",))
    assert find_gdb() is None
