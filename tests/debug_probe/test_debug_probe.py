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
