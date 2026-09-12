# SPDX-License-Identifier: MIT
"""alx.debug_probe.cubeprog - the STM32CubeProgrammer adapter over a faked CLI (no probe, no target).

The fake stands in for subprocess.run: it records the argv the adapter built and, when the argv is a
read, writes the bytes the test wants into the file the adapter asked for. That file hop is the one
structural difference from the Commander adapter - this tool reads into a file rather than onto
stdout - so the fake has to model it or the tests would prove nothing about the code that reads it
back.

Every assertion here is about the ARGV and the error behaviour, which is all that can be checked
without hardware. Whether ST's loader actually erases a sector the other tool cannot is a bench
question, not a unit-test one.

Test group P203-P216 = ALX-1553 CubeProgrammer adapter proofs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alx.debug_probe.cubeprog import EXE_NAME, CubeProg
from alx.errors import ProbeError

MCU = "EXAMPLE-MCU"
# (index, start, length) - a part with four small sectors then a larger one
SECTORS = [
    (0, 0x08000000, 0x4000),
    (1, 0x08004000, 0x4000),
    (2, 0x08008000, 0x4000),
    (3, 0x0800C000, 0x4000),
    (4, 0x08010000, 0x10000),
]


class FakeCli:
    """Stands in for subprocess.run: records argv, and fulfils a read by writing the file."""

    def __init__(
        self,
        stdout: str = "",
        returncode: int = 0,
        read_bytes: bytes = b"",
        writes_file: bool = True,
    ):
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode
        self.read_bytes = read_bytes
        # a tool that reports success and leaves no file is a real failure mode, so it is
        # something the fake has to be able to imitate
        self.writes_file = writes_file

    def __call__(self, argv, capture_output, text, timeout, check):
        self.calls.append(list(argv))
        if "-r" in argv and self.writes_file:
            # -r <addr> <size> <file>: the adapter reads this file back
            out = Path(argv[argv.index("-r") + 3])
            size = int(argv[argv.index("-r") + 2])
            out.write_bytes((self.read_bytes or bytes(size))[:size])
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")

    @property
    def argv(self) -> list[str]:
        return self.calls[-1]

    def argv_of(self, flag: str) -> list[str]:
        return next(c for c in self.calls if flag in c)


class ProbeMaker:
    """probe(stdout="", rc=0, **kw) -> (CubeProg, FakeCli) with run_dir = tmp_path/run."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.run_dir = tmp_path / "run"

    def __call__(self, stdout="", rc=0, read_bytes=b"", **kw):
        fake = FakeCli(stdout, rc, read_bytes)
        self.monkeypatch.setattr(subprocess, "run", fake)
        kw.setdefault("sector_map", SECTORS)
        return CubeProg(self.tmp_path / "tools" / EXE_NAME, MCU, self.run_dir, **kw), fake


@pytest.fixture
def probe(tmp_path, monkeypatch):
    return ProbeMaker(tmp_path, monkeypatch)


# =====================================================================
# P203 - finding the tool
# =====================================================================


def test_ALX1553_P203_the_environment_names_the_tool(tmp_path, monkeypatch):
    exe = tmp_path / EXE_NAME
    exe.write_text("", encoding="ascii")
    monkeypatch.setenv("ALX_HIL_CUBEPROG", str(exe))

    assert CubeProg.find_exe() == exe


def test_ALX1553_P203_an_environment_path_that_does_not_exist_falls_through(tmp_path, monkeypatch):
    """A stale variable must not win over a real installation, and must not raise either."""
    monkeypatch.setenv("ALX_HIL_CUBEPROG", str(tmp_path / "gone" / EXE_NAME))
    monkeypatch.setattr("alx.debug_probe.cubeprog.DEFAULT_EXE_DIRS", ())

    assert CubeProg.find_exe() is None


def test_ALX1553_P203_a_default_installation_is_found(tmp_path, monkeypatch):
    monkeypatch.delenv("ALX_HIL_CUBEPROG", raising=False)
    (tmp_path / EXE_NAME).write_text("", encoding="ascii")
    monkeypatch.setattr("alx.debug_probe.cubeprog.DEFAULT_EXE_DIRS", (tmp_path / "nope", tmp_path))

    assert CubeProg.find_exe() == tmp_path / EXE_NAME


# =====================================================================
# P204 - the connect clause the bench configures
# =====================================================================


def test_ALX1553_P204_the_connect_clause_carries_port_speed_and_reset(probe):
    cube, _ = probe(iface="JLINK", speed_khz=8000, reset_mode="HWrst")

    assert cube.connect_args() == ["-c", "port=JLINK", "freq=8000", "reset=HWrst"]


def test_ALX1553_P204_a_serial_number_is_added_only_when_given(probe):
    without, _ = probe()
    with_sn, _ = probe(serial="1234567890")

    assert not [a for a in without.connect_args() if a.startswith("sn=")]
    assert "sn=1234567890" in with_sn.connect_args()


def test_ALX1553_P204_the_adapter_declares_it_cannot_read_while_the_core_runs(probe):
    """Honestly, per tool: this one halts, so alx.fw.live_watch must not pick it up."""
    cube, _ = probe()

    assert cube.kind == "cubeprog"
    assert cube.mem_while_running is False


# =====================================================================
# P205 - a failed tool run is an error, never a skip
# =====================================================================


def test_ALX1553_P205_a_nonzero_exit_raises(probe):
    cube, _ = probe(stdout="something went wrong", rc=1)

    with pytest.raises(ProbeError, match="reset failed"):
        cube.reset()


@pytest.mark.parametrize(
    "line",
    [
        "Error: No debug probe detected",
        "Error: No STM32 target found",
        "Error: Unable to connect",
        "Cannot connect",
    ],
)
def test_ALX1553_P205_a_connect_failure_raises_even_when_the_exit_code_is_clean(probe, line):
    """The CLI has been seen to exit 0 after failing to find a target, so the text is checked too."""
    cube, _ = probe(stdout=f"...\n{line}\n...", rc=0)

    with pytest.raises(ProbeError, match="reset failed"):
        cube.reset()


def test_ALX1553_P205_the_transcript_is_written_under_the_run_directory(probe):
    cube, _ = probe(stdout="all good")
    cube.reset()

    log = cube.run_dir / "reset.cubeprog.log"
    assert log.exists()
    assert "all good" in log.read_text(encoding="utf-8")


# =====================================================================
# P206 - reset and erase_all
# =====================================================================


def test_ALX1553_P206_reset_asks_for_a_hardware_reset(probe):
    cube, fake = probe()
    cube.reset()

    assert "-hardRst" in fake.argv


def test_ALX1553_P206_erase_all_erases_everything_and_runs(probe):
    cube, fake = probe()
    cube.erase_all()

    assert fake.argv[-3:] == ["-e", "all", "-hardRst"]


def test_ALX1553_P206_holding_leaves_the_core_where_it_is(probe):
    cube, fake = probe()
    cube.erase_all(hold=True)

    assert "-hardRst" not in fake.argv


# =====================================================================
# P207 - erase by SECTOR, which is why this adapter exists
# =====================================================================


def test_ALX1553_P207_a_byte_range_becomes_the_sectors_that_cover_it(probe):
    cube, fake = probe()
    cube.erase(0x08004000, 0x0800BFFF, hold=True)

    assert fake.argv_of("-e")[-3:] == ["-e", "1", "2"]


def test_ALX1553_P207_a_range_that_starts_inside_a_sector_still_erases_it(probe):
    """Erasing is per sector, so a range covering one byte of a sector takes the whole sector."""
    cube, fake = probe()
    cube.erase(0x08004001, 0x08004002, hold=True)

    assert fake.argv_of("-e")[-2:] == ["-e", "1"]


def test_ALX1553_P207_without_a_sector_map_the_adapter_refuses_rather_than_guesses(probe):
    """Erasing the wrong sector is not a recoverable mistake, so there is no default map."""
    cube, _ = probe(sector_map=None)

    with pytest.raises(ProbeError, match="no sector_map"):
        cube.erase(0x08000000, 0x08004000)


def test_ALX1553_P207_a_range_outside_the_map_is_refused(probe):
    cube, _ = probe()

    with pytest.raises(ProbeError, match="covers no sector"):
        cube.erase(0x09000000, 0x09001000)


def test_ALX1553_P207_an_erase_that_does_not_hold_resets_and_runs(probe):
    """The counterpart of the hold=True cases above: the board is left running."""
    cube, fake = probe()

    cube.erase(0x08000000, 0x08003FFF)

    assert fake.argv_of("-e")[-1] == "-hardRst"


def test_ALX1553_P207_erase_reads_back_what_it_was_asked_to(probe):
    cube, _ = probe(read_bytes=b"\xff" * 16)
    result = cube.erase(0x08000000, 0x08003FFF, hold=True, read_back=[0x08000000])

    assert result.read_back[0x08000000] == b"\xff" * 16


# =====================================================================
# P208 - program
# =====================================================================


def test_ALX1553_P208_an_image_that_does_not_exist_is_refused_before_the_tool_runs(probe):
    cube, fake = probe()

    with pytest.raises(ProbeError, match="image not found"):
        cube.program(cube.run_dir / "nothing.bin", 0x08000000)
    assert fake.calls == []


def test_ALX1553_P208_the_image_is_downloaded_at_its_address_and_verified(probe, tmp_path):
    image = tmp_path / "app.bin"
    image.write_bytes(b"\x01\x02\x03\x04")
    cube, fake = probe(stdout="Download verified successfully")

    cube.program(image, 0x08000000, hold=True)

    argv = fake.argv_of("-d")
    assert argv[argv.index("-d") + 2] == "0x08000000"
    assert "-v" in argv


def test_ALX1553_P208_a_verify_the_tool_did_not_confirm_raises(probe, tmp_path):
    image = tmp_path / "app.bin"
    image.write_bytes(b"\x01")
    cube, _ = probe(stdout="Download in progress... done")

    with pytest.raises(ProbeError, match="not verified"):
        cube.program(image, 0x08000000)


def test_ALX1553_P208_verification_can_be_turned_off(probe, tmp_path):
    image = tmp_path / "app.bin"
    image.write_bytes(b"\x01")
    cube, fake = probe(stdout="no verify line here")

    cube.program(image, 0x08000000, verify=False, hold=True)

    assert "-v" not in fake.argv_of("-d")


def test_ALX1553_P208_the_read_back_happens_before_the_reset(probe, tmp_path):
    """What is checked has to be what was programmed, not what a boot left behind."""
    image = tmp_path / "app.bin"
    image.write_bytes(b"\x01")
    cube, fake = probe(stdout="Verification OK", read_bytes=b"\xaa\xbb\xcc\xdd")

    result = cube.program(image, 0x08000000, read_back=4)

    assert result.read_back[0x08000000] == b"\xaa\xbb\xcc\xdd"
    kinds = ["-r" if "-r" in c else "-hardRst" if "-hardRst" in c else "-d" for c in fake.calls]
    assert kinds.index("-r") < kinds.index("-hardRst")


# =====================================================================
# P209 - memory
# =====================================================================


def test_ALX1553_P209_read_mem_returns_the_bytes_the_tool_wrote_to_its_file(probe):
    cube, _ = probe(read_bytes=b"\x11\x22\x33\x44\x55\x66\x77\x88")

    got = cube.read_mem([(0x20000000, 4), (0x20000010, 8)])

    assert got[0x20000000] == b"\x11\x22\x33\x44"
    assert got[0x20000010] == b"\x11\x22\x33\x44\x55\x66\x77\x88"


def test_ALX1553_P209_a_read_that_produced_no_file_raises(probe, monkeypatch):
    cube, _ = probe()
    monkeypatch.setattr(subprocess, "run", FakeCli(writes_file=False))

    with pytest.raises(ProbeError, match="no file written"):
        cube.read_mem([(0x20000000, 4)])


def test_ALX1553_P209_write_mem_writes_one_byte_at_a_time_and_reads_it_back(probe):
    """Byte writes, so bits can be cleared in flash a tool cannot erase - no erase, no reset."""
    data = b"\x00\x00\x00"
    cube, fake = probe(read_bytes=data)

    result = cube.write_mem(0x08100000, data)

    argv = fake.argv_of("-w8")
    assert argv.count("-w8") == 3
    assert argv[argv.index("-w8") + 1] == "0x08100000"
    assert result.read_back[0x08100000] == data
    assert "-hardRst" not in argv


def test_ALX1553_P209_a_write_that_did_not_take_raises(probe):
    cube, _ = probe(read_bytes=b"\xff\xff")

    with pytest.raises(ProbeError, match="read back"):
        cube.write_mem(0x08100000, b"\x00\x00")
