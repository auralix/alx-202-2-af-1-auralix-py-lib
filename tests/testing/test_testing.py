# SPDX-License-Identifier: MIT
"""alx.testing - evidence helpers for pytest suites (no hardware).

Proofs (ALX-1544):
  P61 the collection hook mirrors the proof token of the test NAME and every req marker into user_properties
  P62 git_head gives the short HEAD of a repo and "?" outside one
  P63 run_dir honours ALX_HIL_RUN_DIR, else builds <test_dir>/build/runs/<12-digit timestamp>
  P64 loaded through pytest_plugins (this suite's conftest), the hook tags THIS test with its proof token
"""

import re
from pathlib import Path
from typing import Any

import pytest

import alx.testing as testing
from alx.testing import git_head, pytest_collection_modifyitems, run_dir


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
    items: list[Any] = [
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
    head = git_head(Path(testing.__file__).parent)
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
