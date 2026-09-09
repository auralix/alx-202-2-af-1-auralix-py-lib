# SPDX-License-Identifier: MIT
"""alx.debug_probe - the facade: contract names and the bench's choice of tool (no probe, no target).

Proofs (ALX-1544):
  P70 open() with no kind opens the J-Link adapter bound to mcu, run_dir and the serial from the environment
  P71 an unknown kind raises ProbeError naming the known kinds
  P72 a missing JLink.exe raises ProbeError
  P73 the J-Link adapter satisfies the DebugProbe contract; a class lacking a method does not
  P74 explicit kind / serial / exe / options win over the environment and reach the adapter
  P75 ProbeResult carries the transcript and an empty read-back by default
"""

from pathlib import Path

import pytest

import alx.debug_probe as debug_probe
from alx.debug_probe import DebugProbe, ProbeResult
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
    assert (
        probe.exe == fake_exe and probe.mcu == "EXAMPLE-MCU" and probe.run_dir == tmp_path / "run"
    )
    assert probe.serial == "753000000" and probe.iface == "SWD" and probe.speed_khz == 4000
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "JLINK")
    monkeypatch.delenv("ALX_HIL_DEBUG_PROBE_SN")
    assert debug_probe.open("EXAMPLE-MCU", tmp_path / "run").serial is None, (
        "kind is case-insensitive"
    )


def test_ALX1544_P71_unknown_kind_raises_with_the_known_kinds(fake_exe, tmp_path, monkeypatch):
    with pytest.raises(ProbeError, match="unknown debug probe kind 'stlink' .*known: jlink"):
        debug_probe.open("EXAMPLE-MCU", tmp_path, kind="stlink")
    monkeypatch.setenv("ALX_HIL_DEBUG_PROBE", "nope")
    with pytest.raises(ProbeError, match="unknown debug probe kind 'nope'"):
        debug_probe.open("EXAMPLE-MCU", tmp_path)


def test_ALX1544_P72_missing_tool_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(JLink, "find_exe", staticmethod(lambda env_var="ALX_HIL_JLINK": None))
    monkeypatch.delenv("ALX_HIL_DEBUG_PROBE", raising=False)
    with pytest.raises(ProbeError, match="JLink.exe not found"):
        debug_probe.open("EXAMPLE-MCU", tmp_path)


def test_ALX1544_P73_jlink_satisfies_the_contract(fake_exe, tmp_path):
    assert isinstance(JLink(fake_exe, "EXAMPLE-MCU", tmp_path), DebugProbe)

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
    assert probe.exe == Path(other) and probe.serial == "222"
    assert probe.iface == "JTAG" and probe.speed_khz == 1000


def test_ALX1544_P75_probe_result_defaults():
    r = ProbeResult("transcript")
    assert r.transcript == "transcript" and r.read_back == {}
    assert ProbeResult("t", {1: b"\x00"}).read_back == {1: b"\x00"}
