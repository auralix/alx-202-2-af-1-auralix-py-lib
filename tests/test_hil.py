# SPDX-License-Identifier: MIT
"""alx.hil - the pytest side of a bench suite (no hardware).

Proofs (ALX-1544):
  P60 parse_banner extracts name, version, bin and the 7-char build hash from a boot transcript; {} without one
  P61 the collection hook mirrors the proof token of the test NAME and every req marker into user_properties
  P62 git_head gives the short HEAD of a repo and "?" outside one
  P63 run_dir honours ALX_HIL_RUN_DIR, else builds <test_dir>/build/runs/<12-digit timestamp>
  P64 loaded through pytest_plugins (this suite's conftest), the hook tags THIS test with its proof token
"""

import re
from pathlib import Path

import pytest

import alx.hil as hil
from alx.hil import git_head, parse_banner, pytest_collection_modifyitems, run_dir

BANNER = (
    b"[2000-01-01 00:00:00.028] [INF] APP START\r\n"
    b"[2000-01-01 00:00:00.065] [INF] Example Device FW Started:\r\n"
    b"[2000-01-01 00:00:00.072] [INF] - FW Name: ExampleDeviceFw\r\n"
    b"[2000-01-01 00:00:00.079] [INF] - FW Version: 1.2.3.2609081200.0123456789abcdef0123456789abcdef01234567\r\n"
    b"[2000-01-01 00:00:00.089] [INF] - FW Bin: 2609081200_EX-1_ExampleDeviceFw_V1-2-3_0123456.bin\r\n"
    b"[2000-01-01 00:00:00.100] [INF] \r\n"
)


def test_ALX1544_P60_parse_banner_extracts_the_image_identity():
    ident = parse_banner(BANNER)
    assert ident == {
        "name": "ExampleDeviceFw",
        "ver": "1.2.3.2609081200.0123456789abcdef0123456789abcdef01234567",
        "bin": "2609081200_EX-1_ExampleDeviceFw_V1-2-3_0123456.bin",
        "hash7": "0123456",
    }
    assert parse_banner(b"") == {}
    assert parse_banner(b"[INF] - FW Name: X\r\n") == {}


class _Mark:
    def __init__(self, *args):
        self.args = args


class _Item:
    def __init__(self, name, reqs=()):
        self.name = name
        self.user_properties = []
        self._reqs = reqs

    def iter_markers(self, name):
        return [_Mark(*r) for r in self._reqs] if name == "req" else []


def test_ALX1544_P61_hook_mirrors_proof_token_and_req_markers():
    items = [
        _Item("test_ALX1544_P61_hook", reqs=[("ALX-1600-P3",), ("ALX-1601-P1", "ALX-1601-P2")]),
        _Item("test_plain_name"),
        _Item("test_ALX1234_P20_soak[CR]"),
    ]
    pytest_collection_modifyitems(items)
    assert items[0].user_properties == [
        ("proof", "ALX-1544-P61"),
        ("req", "ALX-1600-P3"),
        ("req", "ALX-1601-P1"),
        ("req", "ALX-1601-P2"),
    ]
    assert items[1].user_properties == []
    assert items[2].user_properties == [("proof", "ALX-1234-P20")]


def test_ALX1544_P62_git_head_of_a_repo_and_outside_one(tmp_path):
    head = git_head(Path(hil.__file__).parent)
    assert re.fullmatch(r"[0-9a-f]{7,}", head), head
    assert git_head(tmp_path) == "?"


def test_ALX1544_P63_run_dir_from_the_launcher_or_a_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("ALX_HIL_RUN_DIR", str(tmp_path / "runs" / "x"))
    assert run_dir(tmp_path) == tmp_path / "runs" / "x"
    monkeypatch.delenv("ALX_HIL_RUN_DIR")
    d = run_dir(tmp_path)
    assert d.parent == tmp_path / "build" / "runs"
    assert re.fullmatch(r"\d{12}", d.name), d.name
    assert not d.exists(), "run_dir only names the directory; the session owner creates it"


@pytest.mark.req("ALX-1544-P64")
def test_ALX1544_P64_plugin_loaded_by_pytest_plugins_tags_this_test(request):
    assert ("proof", "ALX-1544-P64") in request.node.user_properties
    assert ("req", "ALX-1544-P64") in request.node.user_properties
