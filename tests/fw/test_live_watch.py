# SPDX-License-Identifier: MIT
"""alx.fw.live_watch - variables by name over a scripted gdb and a fake probe (no toolchain, no target).

Proofs (ALX-1544):
  P50 names are resolved with one gdb batch (print/x &expr per variable) and a snapshot is one probe
      session, decoded per format
  P51 gdb resolving fewer symbols than asked raises with the gdb output
  P52 a missing ELF or a missing gdb raises before anything is run
  P53 an unknown format is rejected at construction
  P54 decode: little-endian integers of every width, bool from any non-zero byte, rounded f32
  P55 find_gdb: environment variable, PATH, known toolchain locations, else None
  P56 write() encodes the value per format, injects it through probe.write_mem and returns the read-back
  P57 absolute addresses in the table (peripheral registers) need no gdb at all
  P58 read() of one variable; unknown names and values that do not fit the format are refused
"""

import shutil
import struct
import subprocess

import pytest
from hypothesis import given
from hypothesis import strategies as st

import alx.fw.live_watch as live_watch
from alx.debug_probe import ProbeResult
from alx.errors import ProbeError
from alx.fw.live_watch import FORMATS, LiveWatch, decode, encode, find_gdb

VARS = {
    "input_ok": ("app.inputs.ok", "bool"),
    "out_pct": ("app.out.pct", "u8"),
    "volts": ("app.meas.v", "f32"),
}
ADDRS = {"input_ok": 0x20000010, "out_pct": 0x20000011, "volts": 0x20000014}


class FakeGdb:
    def __init__(self, addrs):
        self.calls: list[list[str]] = []
        self.addrs = addrs

    def __call__(self, argv, capture_output, text, timeout, check):
        assert capture_output, "gdb output must be captured to be parsed"
        assert text, "gdb output must be text to be parsed"
        self.calls.append(argv)
        out = "".join(f"${i + 1} = 0x{a:08x}\n" for i, a in enumerate(self.addrs))
        return subprocess.CompletedProcess(
            argv, 0, stdout=out, stderr="warning: no debugging symbols?\n"
        )


class FakeProbe:
    def __init__(self, memory: dict[int, bytes]):
        self.memory = dict(memory)
        self.reads: list[list[tuple[int, int]]] = []
        self.writes: list[tuple[int, bytes]] = []

    def read_mem(self, reads):
        reads = list(reads)
        self.reads.append(reads)
        return {a: self.memory[a][:n] for a, n in reads}

    def write_mem(self, addr, data):
        self.writes.append((addr, data))
        self.memory[addr] = bytes(data)
        return ProbeResult("w1 ...", {addr: bytes(data)})


@pytest.fixture
def elf(tmp_path):
    f = tmp_path / "fw.elf"
    f.write_bytes(b"\x7fELF")
    return f


def make(monkeypatch, elf, tmp_path, memory, addrs=None, variables=VARS):
    gdb = FakeGdb(
        addrs if addrs is not None else [ADDRS["input_ok"], ADDRS["out_pct"], ADDRS["volts"]]
    )
    monkeypatch.setattr(subprocess, "run", gdb)
    probe = FakeProbe(memory)
    return LiveWatch(probe, elf, variables, gdb=tmp_path / "arm-none-eabi-gdb.exe"), probe, gdb


def test_ALX1544_P50_resolve_once_then_snapshot_in_one_probe_session(elf, tmp_path, monkeypatch):
    memory = {0x20000010: b"\x01", 0x20000011: b"\x69", 0x20000014: struct.pack("<f", 11.75)}
    watch, probe, gdb = make(monkeypatch, elf, tmp_path, memory)
    assert len(gdb.calls) == 1
    argv = gdb.calls[0]
    assert argv[0] == str(tmp_path / "arm-none-eabi-gdb.exe")
    assert argv[1] == "--batch"
    assert argv[-1] == str(elf)
    assert argv[2:-1] == [
        "-ex",
        "print/x &app.inputs.ok",
        "-ex",
        "print/x &app.out.pct",
        "-ex",
        "print/x &app.meas.v",
    ]
    assert watch.addr == ADDRS
    assert watch.snapshot() == {"input_ok": True, "out_pct": 105, "volts": 11.75}
    assert probe.reads == [[(0x20000010, 1), (0x20000011, 1), (0x20000014, 4)]]
    assert len(gdb.calls) == 1, "addresses are resolved once, not per snapshot"


def test_ALX1544_P51_partial_symbol_resolution_raises_with_the_gdb_output(
    elf, tmp_path, monkeypatch
):
    with pytest.raises(ProbeError, match="gdb resolved 2 of 3 symbols") as ex:
        make(monkeypatch, elf, tmp_path, {}, addrs=[0x20000010, 0x20000011])
    assert "$2 = 0x20000011" in str(ex.value)
    assert "no debugging symbols" in str(ex.value)
    assert isinstance(ex.value, RuntimeError), "callers catching RuntimeError keep working"


def test_ALX1544_P52_missing_elf_or_gdb_raises_before_running_anything(elf, tmp_path, monkeypatch):
    ran = FakeGdb([])
    monkeypatch.setattr(subprocess, "run", ran)
    with pytest.raises(ProbeError, match="ELF of the flashed build not found"):
        LiveWatch(FakeProbe({}), tmp_path / "nope.elf", VARS, gdb=tmp_path / "gdb")
    monkeypatch.setattr(live_watch, "find_gdb", lambda: None)
    with pytest.raises(ProbeError, match="arm-none-eabi-gdb not found"):
        LiveWatch(FakeProbe({}), elf, VARS)
    assert ran.calls == []


def test_ALX1544_P53_unknown_format_is_rejected(elf, tmp_path):
    with pytest.raises(ValueError, match="bad: unknown format 'u64'"):
        LiveWatch(FakeProbe({}), elf, {"bad": ("x", "u64")}, gdb=tmp_path / "gdb")


@pytest.mark.parametrize(
    ("raw", "fmt", "value"),
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
    monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "onpath" / name))
    assert find_gdb() == tmp_path / "onpath" / "arm-none-eabi-gdb"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(live_watch, "GDB_CANDIDATES", (tmp_path / "missing" / "gdb.exe", gdb))
    assert find_gdb() == gdb
    monkeypatch.setattr(live_watch, "GDB_CANDIDATES", (tmp_path / "missing" / "gdb.exe",))
    assert find_gdb() is None


def test_ALX1544_P56_write_encodes_injects_and_returns_the_read_back(elf, tmp_path, monkeypatch):
    memory = {0x20000010: b"\x00", 0x20000011: b"\x00", 0x20000014: struct.pack("<f", 0.0)}
    watch, probe, _gdb = make(monkeypatch, elf, tmp_path, memory)
    assert watch.write("out_pct", 50) == 50
    assert watch.write("input_ok", True) is True
    assert watch.write("volts", 11.75) == 11.75
    assert probe.writes == [
        (0x20000011, b"\x32"),
        (0x20000010, b"\x01"),
        (0x20000014, struct.pack("<f", 11.75)),
    ]
    assert watch.snapshot() == {"input_ok": True, "out_pct": 50, "volts": 11.75}
    assert encode(-2, "i32") == b"\xfe\xff\xff\xff"
    assert encode(0x1234, "u16") == b"\x34\x12"
    assert encode(0, "bool") == b"\x00"
    assert encode("yes", "bool") == b"\x01"  # type: ignore[arg-type]  # any truthy value is 1


def test_ALX1544_P57_absolute_addresses_need_no_gdb(elf, tmp_path, monkeypatch):
    ran = FakeGdb([])
    monkeypatch.setattr(subprocess, "run", ran)
    monkeypatch.setattr(live_watch, "find_gdb", lambda: None)
    table: dict[str, tuple[str | int, str]] = {
        "gpio_in": (0x41004420, "u32"),
        "status": ("0x40002800", "u8"),
    }
    probe = FakeProbe({0x41004420: b"\x00\x10\x00\x00", 0x40002800: b"\x03"})
    watch = LiveWatch(probe, elf, table)  # no gdb anywhere, still fine
    assert ran.calls == []
    assert watch.addr == {"gpio_in": 0x41004420, "status": 0x40002800}
    assert watch.snapshot() == {"gpio_in": 4096, "status": 3}
    mixed = {"gpio_in": (0x41004420, "u32"), "out_pct": ("app.out.pct", "u8")}
    watch, probe, gdb = make(
        monkeypatch,
        elf,
        tmp_path,
        {0x41004420: b"\x00\x10\x00\x00", 0x20000011: b"\x69"},
        addrs=[0x20000011],
        variables=mixed,
    )
    assert gdb.calls[0][2:-1] == ["-ex", "print/x &app.out.pct"], (
        "only the symbolic entry goes to gdb"
    )
    assert watch.addr == {"gpio_in": 0x41004420, "out_pct": 0x20000011}


def test_ALX1544_P58_read_one_and_refuse_unknown_names_or_unfitting_values(
    elf, tmp_path, monkeypatch
):
    memory = {0x20000010: b"\x01", 0x20000011: b"\x69", 0x20000014: struct.pack("<f", 11.75)}
    watch, probe, _gdb = make(monkeypatch, elf, tmp_path, memory)
    assert watch.read("out_pct") == 105
    assert probe.reads[-1] == [(0x20000011, 1)]
    with pytest.raises(KeyError, match="'nope' is not in the watch table"):
        watch.read("nope")
    with pytest.raises(KeyError, match="'nope' is not in the watch table"):
        watch.write("nope", 1)
    with pytest.raises(ValueError, match="does not fit u8"):
        watch.write("out_pct", 300)
    with pytest.raises(ValueError, match="does not fit u8"):
        watch.write("out_pct", -1)
    assert probe.writes == [], "a refused write never reaches the probe"


INT_RANGES = {
    "u8": (0, 0xFF),
    "i8": (-0x80, 0x7F),
    "u16": (0, 0xFFFF),
    "i16": (-0x8000, 0x7FFF),
    "u32": (0, 0xFFFFFFFF),
    "i32": (-0x80000000, 0x7FFFFFFF),
}


@given(data=st.data(), fmt=st.sampled_from(sorted(INT_RANGES)))
def test_ALX1544_P59_property_integer_formats_round_trip_and_reject_out_of_range(data, fmt):
    lo, hi = INT_RANGES[fmt]
    value = data.draw(st.integers(min_value=lo, max_value=hi))
    raw = encode(value, fmt)
    assert len(raw) == FORMATS[fmt][1]
    assert decode(raw + b"\xaa\xaa", fmt) == value, "decode reads only the format's bytes"
    outside = data.draw(st.sampled_from([lo - 1, hi + 1]))
    with pytest.raises(ValueError, match="does not fit"):
        encode(outside, fmt)


@given(value=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False))
def test_ALX1544_P60_property_f32_round_trips_within_single_precision(value):
    back = decode(encode(value, "f32"), "f32", f32_digits=6)
    assert back == pytest.approx(value, rel=1e-6, abs=1e-6)


def test_ALX1544_P123_known_gdb_locations_are_absolute_gdb_executables():
    assert live_watch.GDB_CANDIDATES, "mutation-driven hardening: the fallback list is not empty"
    for candidate in live_watch.GDB_CANDIDATES:
        assert candidate.is_absolute()
        assert candidate.name.startswith("arm-none-eabi-gdb")
