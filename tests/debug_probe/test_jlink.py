# SPDX-License-Identifier: MIT
"""alx.debug_probe.jlink - the J-Link Commander adapter over a scripted subprocess (no probe, no target).

The fake stands in for subprocess.run: it reads the script the adapter wrote, records argv and timeout,
and returns a canned transcript / exit code.

Proofs (ALX-1544):
  P40 reset: Commander argv (device, interface, speed, autoconnect, no GUI, script) and the script connect/r/g/exit
  P41 a "Cannot connect" transcript or a non-zero exit code raises ProbeError with the transcript tail
  P42 erase: halt, range, optional 16-byte read-backs returned as bytes, reset+go unless hold
  P43 program: forward-slash quoted path at the address, verify line, read-back trimmed, reset+go unless hold
  P44 program raises unless Commander verified; verify=False skips the verifybin line and the check
  P45 parse_mem8 decodes the bytes of one mem8 line and returns None when the address is absent
  P46 read_mem reads every location in one session, trims to n and raises on a missing read
  P47 find_exe: environment variable first, else the newest SEGGER installation, else None
  P48 every script is written under run_dir (created on demand) = the run's evidence
  P56 a probe serial number selects the probe (-SelectEmuBySN), none when not given
  P57 erase_all: reset, halt, erase the whole flash, reset+go unless hold
  P58 write_mem writes byte by byte (w1), reads back and raises on a mismatch
"""

import subprocess
from pathlib import Path

import pytest

import alx.debug_probe.jlink as jlink_mod
from alx.debug_probe import ProbeResult
from alx.debug_probe.jlink import JLink
from alx.errors import ProbeError

MCU = "CORTEX-M0-EXAMPLE"
BLANK16 = "FF " * 15 + "FF"


class FakeCommander:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls = []  # (argv, script text, timeout)
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, capture_output, text, timeout):
        script = Path(argv[argv.index("-CommanderScript") + 1]).read_text(encoding="ascii")
        self.calls.append((argv, script, timeout))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """make(stdout="", rc=0, **jlink_kwargs) -> (JLink, FakeCommander) with run_dir = tmp_path/run."""

    def make(stdout="", rc=0, **kw):
        fc = FakeCommander(stdout, rc)
        monkeypatch.setattr(jlink_mod.subprocess, "run", fc)
        return JLink(tmp_path / "tools" / "JLink.exe", MCU, tmp_path / "run", **kw), fc

    make.run_dir = tmp_path / "run"
    return make


def test_ALX1544_P40_reset_runs_commander_with_the_bench_arguments(probe):
    j, fc = probe(stdout="Reset delay: 0 ms\n")
    result = j.reset()
    assert (
        isinstance(result, ProbeResult)
        and result.transcript == "Reset delay: 0 ms\n"
        and result.read_back == {}
    )
    argv, script, timeout = fc.calls[0]
    assert argv[0] == str(j.exe)
    assert argv[1:11] == [
        "-device",
        MCU,
        "-if",
        "SWD",
        "-speed",
        "4000",
        "-autoconnect",
        "1",
        "-NoGui",
        "1",
    ]
    assert argv[11] == "-CommanderScript" and argv[12] == str(probe.run_dir / "reset.jlink")
    assert script == "connect\nr\ng\nexit\n"
    assert timeout == 30.0
    assert j.kind == "jlink" and j.mem_while_running is True


def test_ALX1544_P41_cannot_connect_or_nonzero_exit_raises(probe):
    j, fc = probe(stdout="Connecting to target via SWD\nCannot connect to target.\n")
    with pytest.raises(ProbeError, match=r"(?s)reset\.jlink failed \(rc=0\).*Cannot connect") as ex:
        j.reset()
    assert "Cannot connect to target." in str(ex.value)
    assert isinstance(ex.value, RuntimeError), "callers catching RuntimeError keep working"
    j, fc = probe(stdout="Script processing completed.\n", rc=1)
    with pytest.raises(ProbeError, match=r"rc=1"):
        j.reset()


def test_ALX1544_P42_erase_range_with_read_back_and_hold(probe):
    j, fc = probe(stdout=f"00010000 = {BLANK16}\n00010100 = {BLANK16}\n")
    result = j.erase(0x00010000, 0x000107FF, read_back=(0x00010000, 0x00010100))
    assert fc.calls[-1][1] == (
        "connect\nh\nerase 0x00010000 0x000107FF\nmem8 0x00010000, 16\nmem8 0x00010100, 16\nr\ng\nexit\n"
    )
    assert result.read_back == {0x00010000: b"\xff" * 16, 0x00010100: b"\xff" * 16}
    j.erase(0x00010000, 0x000107FF, hold=True)
    assert fc.calls[-1][1] == "connect\nh\nerase 0x00010000 0x000107FF\nexit\n"
    assert fc.calls[-1][2] == 60.0
    j, fc = probe(stdout="nothing read\n")
    with pytest.raises(ProbeError, match="erase: read-back at 0x00010000 missing"):
        j.erase(0x00010000, 0x000107FF, read_back=(0x00010000,))


def test_ALX1544_P43_program_quotes_a_forward_slash_path_verifies_and_reads_back(probe, tmp_path):
    binf = tmp_path / "row a.bin"
    binf.write_bytes(b"\x01\x02")
    posix = binf.resolve().as_posix()
    assert "\\" not in posix
    j, fc = probe(stdout=f"Verify successful.\n00010100 = 01 02 {'FF ' * 13}FF\n")
    result = j.program(binf, 0x00010100, read_back=2)
    assert fc.calls[-1][1] == (
        f'connect\nh\nloadbin "{posix}",0x00010100\nverifybin "{posix}",0x00010100\nmem8 0x00010100, 2\nr\ng\nexit\n'
    )
    assert fc.calls[-1][2] == 180.0
    assert result.read_back == {0x00010100: b"\x01\x02"}
    j.program(binf, 0x00010000, hold=True)
    assert (
        fc.calls[-1][1]
        == f'connect\nh\nloadbin "{posix}",0x00010000\nverifybin "{posix}",0x00010000\nexit\n'
    )


def test_ALX1544_P44_program_requires_the_verify_line_unless_verify_is_off(probe, tmp_path):
    img = tmp_path / "image.bin"
    img.write_bytes(b"\x00" * 16)
    posix = img.resolve().as_posix()
    j, fc = probe(stdout="O.K.\n")
    with pytest.raises(ProbeError, match="not verified"):
        j.program(img, 0x08000000)
    assert 'loadbin "' in fc.calls[-1][1] and ",0x08000000" in fc.calls[-1][1]
    result = j.program(img, 0x08000000, verify=False)
    assert "verifybin" not in fc.calls[-1][1] and result.transcript == "O.K.\n"
    assert fc.calls[-1][1] == f'connect\nh\nloadbin "{posix}",0x08000000\nr\ng\nexit\n'


def test_ALX1544_P45_parse_mem8_decodes_one_line_and_none_when_absent():
    out = f"Connecting to target via SWD\n00010000 = {BLANK16}\n20000010 = 01 69 00 00\nScript processing completed.\n"
    assert JLink.parse_mem8(out, 0x00010000) == b"\xff" * 16
    assert JLink.parse_mem8(out, 0x20000010) == b"\x01\x69\x00\x00"
    assert JLink.parse_mem8(out, 0x20000020) is None
    assert JLink.parse_mem8("", 0) is None


def test_ALX1544_P46_read_mem_reads_everything_in_one_session(probe):
    j, fc = probe(stdout="20000010 = 01 69 00 00\n20000014 = 00 00 3C 41\n")
    got = j.read_mem([(0x20000010, 1), (0x20000014, 4)])
    assert len(fc.calls) == 1
    assert fc.calls[0][1] == "connect\nmem8 0x20000010, 1\nmem8 0x20000014, 4\nexit\n"
    assert fc.calls[0][2] == 30.0 and fc.calls[0][0][-1].endswith("mem.jlink")
    assert got == {0x20000010: b"\x01", 0x20000014: b"\x00\x00\x3c\x41"}
    j.read_mem([(0x20000010, 1)], script_name="ram.jlink")
    assert fc.calls[-1][0][-1].endswith("ram.jlink")
    with pytest.raises(ProbeError, match="read_mem: read-back at 0x20000018 missing"):
        j.read_mem([(0x20000010, 1), (0x20000018, 2)])


def test_ALX1544_P47_find_exe_prefers_the_environment_then_the_newest_install(
    tmp_path, monkeypatch
):
    exe = tmp_path / "my" / "JLink.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setenv("ALX_HIL_JLINK", str(exe))
    assert JLink.find_exe() == exe
    monkeypatch.setenv("ALX_HIL_JLINK", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(jlink_mod, "DEFAULT_EXE_DIR", tmp_path / "SEGGER")
    assert JLink.find_exe() is None
    for ver in ("JLink_V790", "JLink_V810", "JLink_V798"):
        (tmp_path / "SEGGER" / ver).mkdir(parents=True)
        (tmp_path / "SEGGER" / ver / "JLink.exe").write_bytes(b"")
    assert JLink.find_exe() == tmp_path / "SEGGER" / "JLink_V810" / "JLink.exe"
    monkeypatch.delenv("ALX_HIL_JLINK")
    assert JLink.find_exe() == tmp_path / "SEGGER" / "JLink_V810" / "JLink.exe"


def test_ALX1544_P48_scripts_land_under_run_dir_created_on_demand(probe):
    j, fc = probe()
    assert not probe.run_dir.exists()
    j.reset()
    j.erase(0, 0xFF)
    assert (probe.run_dir / "reset.jlink").read_text(encoding="ascii") == "connect\nr\ng\nexit\n"
    assert (probe.run_dir / "erase.jlink").exists()


def test_ALX1544_P56_serial_number_selects_the_probe(probe):
    j, fc = probe(serial="753000000")
    j.reset()
    argv = fc.calls[-1][0]
    i = argv.index("-SelectEmuBySN")
    assert argv[i + 1] == "753000000" and argv[i + 2] == "-autoconnect"
    j, fc = probe()
    j.reset()
    assert "-SelectEmuBySN" not in fc.calls[-1][0]


def test_ALX1544_P57_erase_all_erases_the_whole_flash(probe):
    j, fc = probe()
    j.erase_all()
    assert fc.calls[-1][1] == "connect\nr\nh\nerase\nr\ng\nexit\n"
    assert fc.calls[-1][0][-1].endswith("erase_all.jlink")
    j.erase_all(hold=True)
    assert fc.calls[-1][1] == "connect\nr\nh\nerase\nexit\n"


def test_ALX1544_P58_write_mem_writes_bytes_and_reads_them_back(probe):
    j, fc = probe(stdout="20000010 = 01 69 00\n")
    result = j.write_mem(0x20000010, b"\x01\x69\x00")
    assert fc.calls[-1][1] == (
        "connect\nw1 0x20000010, 0x01\nw1 0x20000011, 0x69\nw1 0x20000012, 0x00\nmem8 0x20000010, 3\nexit\n"
    )
    assert result.read_back == {0x20000010: b"\x01\x69\x00"}
    j, fc = probe(stdout="20000010 = 01 00 00\n")
    with pytest.raises(ProbeError, match="write_mem at 0x20000010: read back 010000 != 016900"):
        j.write_mem(0x20000010, b"\x01\x69\x00")
