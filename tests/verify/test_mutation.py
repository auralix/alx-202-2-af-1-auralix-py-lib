# SPDX-License-Identifier: MIT
"""alx.verify.mutation - the mutation driver over a scratch project and a scripted test runner.

Proofs (ALX-1544):
  P106 bytecode(): a syntax error gives None, a comment/whitespace change the same bytecode, a real change differs
  P107 default_tests_for(): pkg/mod -> tests/pkg/test_mod.py, __init__ -> test_<pkg>.py, top-level module,
       fallback to the tests folder
  P108 run_source(): STILLBORN and EQUIVALENT are dropped before any run; rc 0 -> SURVIVED with a diff,
       rc 1 -> KILLED, timeout -> KILLED; the source is restored and re-verified; report + results.json;
       kill rate; sampling with a seed picks a subset
  P109 a red baseline or a red suite after restoring raises MutationError and leaves the source intact
  P110 the real pytest command runs the mirror test file of a scratch project (integration, one mutant)
  P111 universalmutator() generates numerically sorted mutant files; a missing tool is a MutationError
  P112 main() runs every source, prints the per-source counts and the report; a runner failure exits 1
  P114 a run killed while a mutant was planted is repaired by the next start() from the backup copy, and
       start() clears the previous run's mutants, survivors and reports
  P115 a docstring-only mutant is EQUIVALENT (bytecode compared with docstrings stripped)
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from alx.verify import mutation
from alx.verify.mutation import MutationError, MutationRun

SRC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
MUTANTS = {
    "mod.mutant.0.py": SRC.replace("a + b", "a - b"),  # add broken -> the tests catch it
    "mod.mutant.1.py": SRC.replace("a - b", "a + b"),  # sub broken -> nobody tests sub
    "mod.mutant.2.py": SRC.replace("return a + b", "return a + b  # noqa"),  # same bytecode
    "mod.mutant.3.py": SRC.replace("return a + b", "return a + b +"),  # syntax error
    "mod.mutant.4.py": SRC.replace("a + b", "a * b"),  # the runner will hang on this one
}


def project(root: Path) -> Path:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    src = root / "pkg" / "mod.py"
    src.write_text(SRC, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text(
        "from pkg.mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\naddopts = "-p no:randomly"\n',
        encoding="utf-8",
    )
    return src


def fake_generate(source: Path, mutant_dir: Path) -> list[Path]:
    mutant_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in MUTANTS.items():
        (mutant_dir / name).write_text(text, encoding="utf-8")
        paths.append(mutant_dir / name)
    return paths


class ScriptedRunner:
    """Answers each pytest call from the CURRENT content of the planted source, like the suite would."""

    def __init__(self, source: Path):
        self.source = source
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd, capture_output, text, timeout, check, env):
        self.calls.append(cmd)
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        body = self.source.read_text(encoding="utf-8")
        if "a * b" in body:
            raise subprocess.TimeoutExpired(cmd, timeout)
        rc = 0 if "return a + b" in body else 1
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")


def test_ALX1544_P106_bytecode_equivalence_and_stillborn():
    assert mutation.bytecode("def f(:\n", "m.py") is None
    same = mutation.bytecode("x = 1\n", "m.py")
    assert same == mutation.bytecode("x = 1  # comment\n", "m.py")
    assert same != mutation.bytecode("x = 2\n", "m.py")


def test_ALX1544_P115_docstring_only_mutant_is_equivalent():
    original = '"""Module doc."""\n\n\ndef f(a):\n    """Return a."""\n    return a\n'
    doc_mutant = original.replace("Module doc.", "Module True.").replace("Return a.", "Return.")
    assert mutation.bytecode(original, "m.py") == mutation.bytecode(doc_mutant, "m.py")
    assert mutation.bytecode(original, "m.py") != mutation.bytecode(
        original.replace("return a", "return None"), "m.py"
    )


def test_ALX1544_P107_default_tests_for_mirrors_the_package_layout(tmp_path):
    for rel in (
        "tests/pkg/test_mod.py",
        "tests/pkg/test_pkg.py",
        "tests/test_top.py",
        "tests/test_alx.py",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    for rel in (
        "alx/pkg/mod.py",
        "alx/pkg/__init__.py",
        "alx/top.py",
        "alx/pkg/other.py",
        "alx/__init__.py",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    f = mutation.default_tests_for
    assert f(tmp_path, tmp_path / "alx/pkg/mod.py") == tmp_path / "tests/pkg/test_mod.py"
    assert f(tmp_path, tmp_path / "alx/pkg/__init__.py") == tmp_path / "tests/pkg/test_pkg.py"
    assert f(tmp_path, tmp_path / "alx/top.py") == tmp_path / "tests/test_top.py"
    assert f(tmp_path, tmp_path / "alx/__init__.py") == tmp_path / "tests/test_alx.py"
    assert f(tmp_path, tmp_path / "alx/pkg/other.py") == tmp_path / "tests"


def test_ALX1544_P114_start_recovers_a_planted_source_and_clears_the_previous_run(tmp_path):
    src = project(tmp_path)
    out = tmp_path / "out"
    # a previous run died mid-plant: the mutant sits in the tree, the original in the backup
    (out / "backup" / "pkg").mkdir(parents=True)
    (out / "backup" / "pkg" / "mod.py").write_text(SRC, encoding="utf-8")
    src.write_text(MUTANTS["mod.mutant.0.py"], encoding="utf-8")
    (out / "mutants" / "mod").mkdir(parents=True)
    (out / "mutants" / "mod" / "mod.mutant.0.py").write_text("old", encoding="utf-8")
    (out / "survivors").mkdir()
    (out / "report.txt").write_text("old", encoding="utf-8")

    run = MutationRun(tmp_path, out, generate=fake_generate, run=ScriptedRunner(src))
    assert run.start() == ["pkg/mod.py"]
    assert src.read_text(encoding="utf-8") == SRC, "the original is back"
    assert not (out / "backup").exists()
    assert not (out / "mutants").exists()
    assert not (out / "survivors").exists()
    assert not (out / "report.txt").exists()
    assert run.start() == [], "nothing to recover the second time"

    # a run that died between writing the backup and planting: the copy equals the source
    (out / "backup" / "pkg").mkdir(parents=True)
    (out / "backup" / "pkg" / "mod.py").write_text(SRC, encoding="utf-8")
    assert run.start() == []
    assert not (out / "backup").exists()

    results = run.run_source(src)  # a normal run leaves no backup behind
    assert len(results) == 3
    assert not (out / "backup").exists()


def test_ALX1544_P108_run_source_classifies_restores_and_reports(tmp_path):
    src = project(tmp_path)
    stale = tmp_path / "pkg" / "__pycache__" / "mod.cpython-310.pyc"
    stale.parent.mkdir()
    stale.write_bytes(b"stale")
    runner = ScriptedRunner(src)
    run = MutationRun(tmp_path, tmp_path / "out", generate=fake_generate, run=runner)
    results = run.run_source(src)
    assert not stale.parent.exists(), "stale bytecode is removed before the first plant"

    assert [(r.mutant, r.status, r.detail) for r in results] == [
        ("mod.mutant.0.py", "KILLED", "rc=1"),
        ("mod.mutant.1.py", "SURVIVED", ""),
        ("mod.mutant.4.py", "KILLED", "timeout"),
    ]
    assert run.counts() == {"KILLED": 2, "SURVIVED": 1, "STILLBORN": 1, "EQUIVALENT": 1}
    assert run.kill_rate() == pytest.approx(200 / 3)
    assert src.read_text(encoding="utf-8") == SRC, "the original is restored"
    assert len(runner.calls) == 5, "baseline + 3 viable mutants + restoration proof"
    assert runner.calls[0][-1].endswith("test_mod.py"), "the mirror test file, not the whole folder"

    diff = (tmp_path / "out" / "survivors" / "mod.mutant.1.diff").read_text(encoding="utf-8")
    assert "-    return a - b" in diff
    assert "+    return a + b" in diff

    text = run.report()
    assert "killed 2  survived 1  stillborn 1  equivalent 1" in text
    assert "kill rate: 66.7%" in text
    assert "SURVIVED  pkg/mod.py  mod.mutant.1.py  -> survivors/mod.mutant.1.diff" in text
    results_json = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert {r["status"] for r in results_json} == {"KILLED", "SURVIVED", "STILLBORN", "EQUIVALENT"}

    sampled = MutationRun(
        tmp_path, tmp_path / "out2", sample=2, seed=7, generate=fake_generate, run=runner
    )
    assert len(sampled.run_source(src)) == 2
    assert MutationRun(tmp_path, tmp_path / "out3", run=runner).kill_rate() is None


def test_ALX1544_P109_red_baseline_or_red_restore_raise_and_keep_the_source(tmp_path):
    src = project(tmp_path)

    def red(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    with pytest.raises(MutationError, match="baseline is red"):
        MutationRun(tmp_path, tmp_path / "out", generate=fake_generate, run=red).run_source(src)
    assert src.read_text(encoding="utf-8") == SRC

    calls = {"n": 0}

    def red_after(cmd, **kw):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 0 if calls["n"] == 1 else 1, stdout="", stderr="")

    with pytest.raises(MutationError, match="suite red after restoring"):
        MutationRun(tmp_path, tmp_path / "out", generate=lambda s, d: [], run=red_after).run_source(
            src
        )
    assert src.read_text(encoding="utf-8") == SRC


@pytest.mark.timeout(180)
def test_ALX1544_P110_real_pytest_runs_the_mirror_test_of_a_scratch_project(tmp_path):
    src = project(tmp_path)

    def one_mutant(source: Path, mutant_dir: Path) -> list[Path]:
        mutant_dir.mkdir(parents=True, exist_ok=True)
        killed = mutant_dir / "mod.mutant.0.py"
        killed.write_text(MUTANTS["mod.mutant.0.py"], encoding="utf-8")
        survivor = mutant_dir / "mod.mutant.1.py"
        survivor.write_text(MUTANTS["mod.mutant.1.py"], encoding="utf-8")
        return [killed, survivor]

    run = MutationRun(tmp_path, tmp_path / "out", generate=one_mutant)
    results = run.run_source(src)
    assert [(r.mutant, r.status) for r in results] == [
        ("mod.mutant.0.py", "KILLED"),
        ("mod.mutant.1.py", "SURVIVED"),
    ]
    assert src.read_text(encoding="utf-8") == SRC
    assert not list((tmp_path / "pkg").rglob("__pycache__")), "no stale bytecode left behind"


@pytest.mark.timeout(120)
def test_ALX1544_P111_universalmutator_generates_sorted_mutants_and_reports_a_missing_tool(
    tmp_path, monkeypatch
):
    src = project(tmp_path)
    mutants = mutation.universalmutator(src, tmp_path / "mut")
    numbers = [int(p.suffixes[-2][1:]) for p in mutants]
    assert numbers
    assert numbers == sorted(numbers)
    assert all(p.exists() and p.name.startswith("mod.mutant.") for p in mutants)
    assert any("a - b" in p.read_text(encoding="utf-8") for p in mutants), (
        "an arithmetic mutant exists"
    )

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python.exe"))
    with pytest.raises(MutationError, match="mutate not found"):
        mutation.universalmutator(src, tmp_path / "mut2")

    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(MutationError, match="mutate failed"):
        mutation.universalmutator(src, tmp_path / "mut3")


def test_ALX1544_P112_main_runs_every_source_and_reports_or_fails(tmp_path, monkeypatch, capsys):
    class FakeRun:
        def __init__(self, root, out, sample=0, seed=1):
            self.args = (root, out, sample, seed)

        def run_source(self, source):
            if source.name == "bad.py":
                raise MutationError("baseline is red for bad.py")
            return [
                mutation.Outcome("x", "m0", "KILLED", 0.1),
                mutation.Outcome("x", "m1", "SURVIVED", 0.1),
            ]

        def report(self):
            return "REPORT\n"

        def start(self):
            return ["alx/left.py"]

    monkeypatch.setattr(mutation, "MutationRun", FakeRun)
    argv = ["--root", str(tmp_path), "--out", "o", "--sample", "5", "alx/ok.py"]
    assert mutation.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("RECOVERED alx/left.py")
    assert "alx/ok.py: 1 killed, 1 survived" in out
    assert out.endswith("REPORT\n")
    assert mutation.main(["--root", str(tmp_path), "alx/bad.py"]) == 1
    assert "MUTATION RUN FAILED: baseline is red for bad.py" in capsys.readouterr().out
