# SPDX-License-Identifier: MIT
"""alx.verify.evidence - the evidence of a pytest run (no hardware).

Proofs (ALX-1544):
  P61 the collection hook mirrors the proof token of the test NAME and every req marker into user_properties
  P62 git_head gives the short HEAD of a repo and "?" outside one
  P342 git_head marks a tree whose tracked files differ from HEAD with -dirty; an untracked file
       does not count, a staged change does (ALX-1564)
  P63 run_dir honours ALX_HIL_RUN_DIR, else builds <test_dir>/build/runs/<12-digit timestamp>
  P64 loaded through pytest_plugins (this suite's conftest), the hook tags THIS test with its proof token
  P148 pytest-randomly's seed is recorded as the junit testsuite property randomly_seed; without the plugin
       nothing is recorded (the seed policy: random every run, always in the evidence, never fixed)
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

import alx.verify.evidence as evidence
from alx.verify.evidence import git_head, pytest_collection_modifyitems, run_dir


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
    head = git_head(Path(evidence.__file__).parent)
    assert re.fullmatch(r"[0-9a-f]{7,}(-dirty)?", head), head
    assert git_head(tmp_path) == "?"


IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@example.invalid")


def _git(repo, *args):
    subprocess.run(  # noqa: S603 - fixed argv, no shell; builds a throwaway repository
        ["git", "-C", str(repo), *IDENTITY, *args],  # noqa: S607 - the developer's own git, as in the module
        capture_output=True,
        check=True,
    )


def test_ALX1564_P342_git_head_marks_a_tree_whose_tracked_files_differ_from_head(
    tmp_path, monkeypatch
):
    # the developer's own git configuration (signing, hooks, line endings) stays out of the repository
    (tmp_path / "gitconfig").write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_bytes(b"1\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    clean = git_head(repo)
    assert re.fullmatch(r"[0-9a-f]{7,}", clean), clean
    (repo / "b.txt").write_bytes(b"new\n")
    assert git_head(repo) == clean, "an untracked file is not a change to what HEAD names"
    (repo / "a.txt").write_bytes(b"2\n")
    assert git_head(repo) == f"{clean}-dirty", "a modified tracked file marks the head"
    _git(repo, "add", "a.txt")
    assert git_head(repo) == f"{clean}-dirty", "so does a staged one"


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


def _nested_run(pytester, *args, junit=True):
    pytester.makepyfile(test_nested="def test_ALX1544_P148_nested():\n    assert True\n")
    report = pytester.path / "junit.xml"
    report.unlink(missing_ok=True)
    junit_args = ["-o", "junit_family=xunit1", f"--junitxml={report}"] if junit else []
    # in-process, so the plugin code the nested session runs counts for this suite's coverage
    result = pytester.runpytest_inprocess("-p", "alx.verify.evidence", *junit_args, *args)
    result.assert_outcomes(passed=1)
    return report.read_text(encoding="utf-8") if junit else ""


def test_ALX1544_P148_random_seed_recorded_as_testsuite_property_only_when_randomly_is_active(
    pytester,
):
    with_seed = _nested_run(pytester, "-p", "randomly", "--randomly-seed=4711")
    assert '<property name="randomly_seed" value="4711"' in with_seed
    assert '<property name="proof" value="ALX-1544-P148"' in with_seed
    without = _nested_run(pytester, "-p", "no:randomly")
    assert "randomly_seed" not in without
    _nested_run(
        pytester, "-p", "randomly", "--randomly-seed=4711", junit=False
    )  # no report: nothing to record
