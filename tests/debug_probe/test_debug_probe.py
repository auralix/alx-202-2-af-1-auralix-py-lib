# SPDX-License-Identifier: MIT
"""alx.debug_probe - the facade: contract names and the bench's choice of tool (no probe, no target).

Proofs (ALX-1544):
  P70 open() with no kind opens the J-Link adapter bound to mcu, run_dir and the serial from the environment
  P71 an unknown kind raises ProbeError naming the known kinds
  P72 a missing JLink.exe raises ProbeError
  P73 the J-Link adapter satisfies the DebugProbe contract; a class lacking a method does not
  P74 explicit kind / serial / exe / options win over the environment and reach the adapter
  P75 ProbeResult carries the transcript and an empty read-back by default

Proofs (ALX-1553):
  P210 kind='cubeprog' opens the CubeProgrammer adapter, bound the same way as the other one
  P211 a missing STM32_Programmer_CLI raises ProbeError naming the variable that overrides it
  P212 the CubeProgrammer adapter satisfies the DebugProbe contract, and the two are
       interchangeable behind it
  P213 the target's sector map reaches the adapter that erases by sector, and a bench that
       describes its part does not break the adapter that does not need it
"""

from pathlib import Path

import pytest

import alx.debug_probe as debug_probe
from alx.debug_probe import DebugProbe, ProbeResult
from alx.debug_probe.cubeprog import CubeProg
from alx.debug_probe.jlink import JLink
from alx.errors import ProbeError


@pytest.fixture
def fake_exe(tmp_path, monkeypatch):
    exe = tmp_path / "JLink.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(JLink, "find_exe", staticmethod(lambda env_var="ALX_HIL_JLINK": exe))
    monkeypatch.delenv("ALX_HIL_DEBUG_PROBE", raising=False)
    monkeypatch.delenv("ALX_HIL_DEBUG_PROBE_SN", raising=False)
    return exe


def test_ALX1544_P70_open_defaults_to_jlink_and_binds_mcu_run_dir_and_serial(
    fake_exe, tmp_path, monkeypatch
):
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE_SN", "753000000")
    probe = debug_probe.open("EXAMPLE-MCU", tmp_path / "run")
    assert isinstance(probe, JLink)
    assert probe.exe == fake_exe
    assert probe.mcu == "EXAMPLE-MCU"
    assert probe.run_dir == tmp_path / "run"
    assert probe.serial == "753000000"
    assert probe.iface == "SWD"
    assert probe.speed_khz == 4000
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "JLINK")
    monkeypatch.delenv("ALX_HIL_DEBUG_PROBE_SN")
    upper = debug_probe.open("EXAMPLE-MCU", tmp_path / "run")
    assert isinstance(upper, JLink), "kind is case-insensitive"
    assert upper.serial is None


def test_ALX1544_P71_unknown_kind_raises_with_the_known_kinds(fake_exe, tmp_path, monkeypatch):
    with pytest.raises(ProbeError, match=r"unknown debug probe kind 'stlink' .*known: jlink"):
        debug_probe.open("EXAMPLE-MCU", tmp_path, kind="stlink")
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "nope")
    with pytest.raises(ProbeError, match="unknown debug probe kind 'nope'"):
        debug_probe.open("EXAMPLE-MCU", tmp_path)


def test_ALX1544_P72_missing_tool_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(JLink, "find_exe", staticmethod(lambda env_var="ALX_HIL_JLINK": None))
    monkeypatch.delenv("ALX_HIL_DEBUG_PROBE", raising=False)
    with pytest.raises(ProbeError, match=r"JLink\.exe not found"):
        debug_probe.open("EXAMPLE-MCU", tmp_path)


def test_ALX1544_P73_jlink_satisfies_the_contract(fake_exe, tmp_path):
    assert isinstance(JLink(fake_exe, "EXAMPLE-MCU", tmp_path), DebugProbe)


@pytest.fixture
def fake_cube_exe(tmp_path, monkeypatch):
    exe = tmp_path / "STM32_Programmer_CLI.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(CubeProg, "find_exe", staticmethod(lambda env_var="ALX_HIL_CUBEPROG": exe))
    monkeypatch.delenv("ALX_HIL_DEBUG_PROBE_SN", raising=False)
    return exe


def test_ALX1553_P210_kind_cubeprog_opens_the_cubeprogrammer_adapter(
    fake_cube_exe, tmp_path, monkeypatch
):
    """The bench picks the tool by name; everything else is bound exactly as for the other."""
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "cubeprog")
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE_SN", "753000000")

    probe = debug_probe.open("EXAMPLE-MCU", tmp_path / "run")

    assert isinstance(probe, CubeProg)
    assert probe.exe == fake_cube_exe
    assert probe.mcu == "EXAMPLE-MCU"
    assert probe.run_dir == tmp_path / "run"
    assert probe.serial == "753000000"
    assert probe.iface == "JLINK"
    assert probe.kind in debug_probe.KNOWN_KINDS


def test_ALX1553_P211_a_missing_cubeprogrammer_raises_and_names_the_override(tmp_path, monkeypatch):
    monkeypatch.setattr(CubeProg, "find_exe", staticmethod(lambda env_var="ALX_HIL_CUBEPROG": None))
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "cubeprog")

    with pytest.raises(ProbeError, match="ALX_HIL_CUBEPROG"):
        debug_probe.open("EXAMPLE-MCU", tmp_path)


def test_ALX1553_P212_both_adapters_satisfy_the_same_contract(fake_cube_exe, tmp_path):
    """Which is the whole point of the facade: a caller never names a tool."""
    assert isinstance(CubeProg(fake_cube_exe, "EXAMPLE-MCU", tmp_path), DebugProbe)

    class Incomplete:
        kind = "x"
        mem_while_running = False

        def reset(self):
            return ProbeResult("")

    assert not isinstance(Incomplete(), DebugProbe)


def test_ALX1544_P74_explicit_arguments_win_and_options_reach_the_adapter(
    fake_exe, tmp_path, monkeypatch
):
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "nope")
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE_SN", "111")
    other = tmp_path / "other" / "JLink.exe"
    probe = debug_probe.open(
        "EXAMPLE-MCU", tmp_path, kind="jlink", serial="222", exe=other, iface="JTAG", speed_khz=1000
    )
    assert isinstance(probe, JLink)
    assert probe.exe == Path(other)
    assert probe.serial == "222"
    assert probe.iface == "JTAG"
    assert probe.speed_khz == 1000


def test_ALX1544_P75_probe_result_defaults():
    r = ProbeResult("transcript")
    assert r.transcript == "transcript"
    assert r.read_back == {}
    assert ProbeResult("t", {1: b"\x00"}).read_back == {1: b"\x00"}


# the four smallest sectors of a part that starts with them - shape only, no real part named
SECTORS = [(0, 0x08000000, 0x4000), (1, 0x08004000, 0x4000), (2, 0x08008000, 0x4000)]


def test_ALX1553_P213_the_sector_map_reaches_the_adapter_that_erases_by_sector(
    fake_cube_exe, tmp_path, monkeypatch
):
    """The layout is the TARGET's, so the bench states it once and open() routes it.

    Without this the sector-erasing adapter refuses every ranged erase, which is not a theoretical
    state: it is what the bench actually hit the first time it selected this tool.
    """
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "cubeprog")

    probe = debug_probe.open("EXAMPLE-MCU", tmp_path, sector_map=SECTORS)

    # narrowed deliberately: sector_map is NOT on the DebugProbe contract and must not be, or every
    # caller would start reaching for it. It belongs to the adapter that needs it, and mypy is the
    # thing that keeps that true.
    assert isinstance(probe, CubeProg)
    assert probe.sector_map == SECTORS
    assert probe._sectors_for(0x08004000, 0x0800BFFF) == [1, 2]


def test_ALX1553_P213_a_bench_that_states_its_layout_still_opens_a_range_erasing_tool(
    fake_exe, tmp_path, monkeypatch
):
    """A bench must not have to ask which tool it is about to get.

    open() takes the map as its own argument rather than through **options precisely so that the
    SAME call works for both kinds - forwarding it blindly would make this a TypeError and force
    the caller into `if kind == ...`, which is the thing this package exists to prevent.
    """
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "jlink")

    probe = debug_probe.open("EXAMPLE-MCU", tmp_path, sector_map=SECTORS)

    assert isinstance(probe, JLink)
    assert not hasattr(probe, "sector_map"), "a tool that erases a byte range never sees the map"
