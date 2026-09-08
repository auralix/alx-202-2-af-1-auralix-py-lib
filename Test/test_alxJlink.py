"""alxJlink - the J-Link Commander wrapper over a scripted subprocess (no probe, no target).

The fake stands in for subprocess.run: it reads the script the wrapper wrote, records argv and
timeout, and returns a canned transcript / exit code.

Proofs (ALX-1544):
  P40 reset: Commander argv (device, interface, speed, autoconnect, no GUI, script) and the script connect/r/g/exit
  P41 a "Cannot connect" transcript or a non-zero exit code raises with the transcript tail
  P42 erase: halt, range, optional 16-byte read-backs, reset+go unless hold
  P43 loadbin: forward-slash quoted path at the address, optional read-back, reset+go unless hold
  P44 flash: reset, halt, loadbin, verifybin, reset, go - and it raises unless Commander verified
  P45 parse_mem8 decodes the bytes of one mem8 line and returns None when the address is absent
  P46 read_mem8 reads every location in one session, trims to n and raises on a missing read
  P47 find_exe: environment variable first, else the newest SEGGER installation, else None
  P48 every script is written under run_dir (created on demand) = the run's evidence
"""

import subprocess
from pathlib import Path

import pytest

import alxJlink
from alxJlink import JLink

DEVICE = "CORTEX-M0-EXAMPLE"


class FakeCommander:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls = []               # (argv, script text, timeout)
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, capture_output, text, timeout):
        script = Path(argv[argv.index("-CommanderScript") + 1]).read_text(encoding="ascii")
        self.calls.append((argv, script, timeout))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """make(stdout="", rc=0) -> (JLink, FakeCommander) with run_dir = tmp_path/run."""
    def make(stdout="", rc=0):
        fc = FakeCommander(stdout, rc)
        monkeypatch.setattr(alxJlink.subprocess, "run", fc)
        return JLink(tmp_path / "tools" / "JLink.exe", DEVICE, tmp_path / "run"), fc
    make.run_dir = tmp_path / "run"
    return make


def test_ALX1544_P40_reset_runs_commander_with_the_bench_arguments(probe):
    j, fc = probe(stdout="Reset delay: 0 ms\n")
    assert j.reset() == "Reset delay: 0 ms\n"
    argv, script, timeout = fc.calls[0]
    assert argv[0] == str(j.exe)
    assert argv[1:11] == ["-device", DEVICE, "-if", "SWD", "-speed", "4000", "-autoconnect", "1", "-NoGui", "1"]
    assert argv[11] == "-CommanderScript" and argv[12] == str(probe.run_dir / "reset.jlink")
    assert script == "connect\nr\ng\nexit\n"
    assert timeout == 30.0


def test_ALX1544_P41_cannot_connect_or_nonzero_exit_raises(probe):
    j, fc = probe(stdout="Connecting to target via SWD\nCannot connect to target.\n")
    with pytest.raises(RuntimeError, match=r"(?s)reset\.jlink failed \(rc=0\).*Cannot connect") as ex:
        j.reset()
    assert "Cannot connect to target." in str(ex.value)
    j, fc = probe(stdout="Script processing completed.\n", rc=1)
    with pytest.raises(RuntimeError, match=r"rc=1"):
        j.reset()


def test_ALX1544_P42_erase_range_with_read_back_and_hold(probe):
    j, fc = probe()
    j.erase(0x00010000, 0x000107FF, read_back=(0x00010000, 0x00010100))
    assert fc.calls[-1][1] == ("connect\nh\nerase 0x00010000 0x000107FF\n"
                               "mem8 0x00010000, 16\nmem8 0x00010100, 16\nr\ng\nexit\n")
    j.erase(0x00010000, 0x000107FF, hold=True)
    assert fc.calls[-1][1] == "connect\nh\nerase 0x00010000 0x000107FF\nexit\n"
    assert fc.calls[-1][2] == 60.0


def test_ALX1544_P43_loadbin_quotes_a_forward_slash_path_at_the_address(probe, tmp_path):
    j, fc = probe()
    binf = tmp_path / "row a.bin"
    binf.write_bytes(b"\x01\x02")
    j.loadbin(binf, 0x00010100, read_back=16)
    posix = binf.resolve().as_posix()
    assert "\\" not in posix
    assert fc.calls[-1][1] == f'connect\nh\nloadbin "{posix}",0x00010100\nmem8 0x00010100, 16\nr\ng\nexit\n'
    j.loadbin(binf, 0x00010000, hold=True)
    assert fc.calls[-1][1] == f'connect\nh\nloadbin "{posix}",0x00010000\nexit\n'


def test_ALX1544_P44_flash_programs_verifies_and_requires_the_verify_line(probe, tmp_path):
    img = tmp_path / "image.bin"
    img.write_bytes(b"\x00" * 16)
    posix = img.resolve().as_posix()
    j, fc = probe(stdout="O.K.\nVerify successful.\n")
    assert "Verify successful" in j.flash(img)
    assert fc.calls[-1][1] == (f'connect\nr\nh\nloadbin "{posix}",0x00000000\n'
                               f'verifybin "{posix}",0x00000000\nr\ng\nexit\n')
    assert fc.calls[-1][2] == 180.0
    j, fc = probe(stdout="O.K.\n")
    with pytest.raises(RuntimeError, match="not verified"):
        j.flash(img, addr=0x08000000)
    assert 'loadbin "' in fc.calls[-1][1] and ",0x08000000" in fc.calls[-1][1]


def test_ALX1544_P45_parse_mem8_decodes_one_line_and_none_when_absent():
    out = ("Connecting to target via SWD\n"
           "00010000 = FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF FF\n"
           "20000010 = 01 69 00 00\n"
           "Script processing completed.\n")
    assert JLink.parse_mem8(out, 0x00010000) == b"\xff" * 16
    assert JLink.parse_mem8(out, 0x20000010) == b"\x01\x69\x00\x00"
    assert JLink.parse_mem8(out, 0x20000020) is None
    assert JLink.parse_mem8("", 0) is None


def test_ALX1544_P46_read_mem8_reads_everything_in_one_session(probe):
    j, fc = probe(stdout="20000010 = 01 69 00 00\n20000014 = 00 00 3C 41\n")
    got = j.read_mem8([(0x20000010, 1), (0x20000014, 4)])
    assert len(fc.calls) == 1
    assert fc.calls[0][1] == "connect\nmem8 0x20000010, 1\nmem8 0x20000014, 4\nexit\n"
    assert fc.calls[0][2] == 30.0 and fc.calls[0][0][-1].endswith("mem8.jlink")
    assert got == {0x20000010: b"\x01", 0x20000014: b"\x00\x00\x3c\x41"}
    j.read_mem8([(0x20000010, 1)], script_name="ram.jlink")
    assert fc.calls[-1][0][-1].endswith("ram.jlink")
    with pytest.raises(RuntimeError, match="read at 0x20000018 failed"):
        j.read_mem8([(0x20000010, 1), (0x20000018, 2)])


def test_ALX1544_P47_find_exe_prefers_the_environment_then_the_newest_install(tmp_path, monkeypatch):
    exe = tmp_path / "my" / "JLink.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setenv("ALX_HIL_JLINK", str(exe))
    assert JLink.find_exe() == exe
    monkeypatch.setenv("ALX_HIL_JLINK", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(alxJlink, "DEFAULT_EXE_DIR", tmp_path / "SEGGER")
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
