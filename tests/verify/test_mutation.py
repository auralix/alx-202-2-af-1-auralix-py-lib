# SPDX-License-Identifier: MIT
"""alx.verify.mutation - the mutation driver over a scratch project and a scripted test runner.

Proofs (ALX-1544):
  P106 fingerprint(): a syntax error gives None, a comment/whitespace change the same fingerprint, a real change differs
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
  P115 a docstring-only mutant is EQUIVALENT (fingerprint ignores docstrings)
  P131 mutation-driven hardening: an inserted line, a changed annotation and a moved position are EQUIVALENT;
       main() passes --sample/--seed on and defaults sample to 0
  P134 hooks for other languages: check fails -> STILLBORN, equal fingerprint -> EQUIVALENT, rebuild fails ->
       KILLED_COMPILE (scored apart from the kill rate), rebuild runs again after the restore and its failure
       is a MutationError
  P135 a root-level C source with a Test/ folder: mirror test_<stem>.py, suffix-aware mutant files, dotted key
  P136 the command hooks from templates ({mutant} / {source} replaced, run in the root): check, fingerprint,
       rebuild; main() wires --tests-dir, --check-cmd, --fingerprint-cmd, --rebuild-cmd
  P182 a run where the generator produced mutants and NOT ONE reached the tests says so and exits non-zero:
       a broken check hook must not read as a lane that passed
  P186 statements that do nothing are dropped from the fingerprint (a docstring replaced by pass, a bare ...,
       a continue ending a loop body) while a break, a continue that is not last, and any real change are kept
  P188 a continue in TAIL position of a loop is dropped too - at the end of an if arm or a with that is itself
       last - while one inside a nested loop or a try, or not last, is kept
  P189 the mutant pool is generated and filtered ONCE per source: reused while the source and the hooks are
       unchanged (same verdicts, no second generation), rebuilt when either changes, skipped with pool=False
"""

import json
import re
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


def test_ALX1544_P106_fingerprint_equivalence_and_stillborn():
    assert mutation.fingerprint("def f(:\n", "m.py") is None
    same = mutation.fingerprint("x = 1\n", "m.py")
    assert same == mutation.fingerprint("x = 1  # comment\n", "m.py")
    assert same != mutation.fingerprint("x = 2\n", "m.py")


def test_ALX1544_P115_docstring_only_mutant_is_equivalent():
    original = '"""Module doc."""\n\n\ndef f(a):\n    """Return a."""\n    return a\n'
    doc_mutant = original.replace("Module doc.", "Module True.").replace("Return a.", "Return.")
    assert mutation.fingerprint(original, "m.py") == mutation.fingerprint(doc_mutant, "m.py")
    assert mutation.fingerprint(original, "m.py") != mutation.fingerprint(
        original.replace("return a", "return None"), "m.py"
    )


def test_ALX1544_P131_inserted_lines_annotations_and_positions_are_equivalent():
    original = (
        '"""Doc.\n\nmore doc\n"""\n\nfrom __future__ import annotations\n\n'
        "X: int = 1\nY: list[str]\n\n\nclass C:\n"
        '    """Class doc."""\n\n    n: int = 2\n\n'
        "    def f(self, a: int, *v: str, k: bool = False, **kw: int) -> dict[str, int]:\n"
        '        """Doc."""\n        return {"a": a}\n\n'
        "    async def g(self, a: int) -> None:\n"
        '        """Doc."""\n        return None\n'
    )
    base = mutation.fingerprint(original, "m.py")
    assert base is not None
    assert base == mutation.fingerprint(
        original.replace("\nmore doc\n", "\nbreak;\nmore doc\n"), "m.py"
    ), "a line inserted inside a docstring shifts every position: still equivalent"
    assert base == mutation.fingerprint(
        original.replace("-> dict[str, int]", "-> dict[int]"), "m.py"
    )
    assert base == mutation.fingerprint(original.replace("a: int,", "a: float,"), "m.py")
    assert base == mutation.fingerprint(original.replace("g(self, a: int)", "g(self, a)"), "m.py")
    assert base == mutation.fingerprint(original.replace("X: int = 1", "X = 1"), "m.py")
    assert base == mutation.fingerprint(original.replace("Y: list[str]\n", ""), "m.py"), (
        "a bare declaration runs nothing"
    )
    assert base != mutation.fingerprint(original.replace("X: int = 1", "X: int = 2"), "m.py")
    assert base != mutation.fingerprint(original.replace("return {", "return dict({"), "m.py")
    assert base != mutation.fingerprint(original.replace("return None", "return 1"), "m.py")


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

    # a planted mutant SHORTER than the original is recovered too (not a byte-order comparison)
    (out / "backup" / "pkg").mkdir(parents=True)
    (out / "backup" / "pkg" / "mod.py").write_text(SRC, encoding="utf-8")
    src.write_text("x = 1\n", encoding="utf-8")
    assert run.start() == ["pkg/mod.py"]
    assert src.read_text(encoding="utf-8") == SRC

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
    assert run.counts() == {
        "KILLED": 2,
        "KILLED_COMPILE": 0,
        "SURVIVED": 1,
        "STILLBORN": 1,
        "EQUIVALENT": 1,
    }
    assert run.kill_rate() == pytest.approx(200 / 3)
    assert src.read_text(encoding="utf-8") == SRC, "the original is restored"
    assert len(runner.calls) == 5, "baseline + 3 viable mutants + restoration proof"
    assert runner.calls[0][-1].endswith("test_mod.py"), "the mirror test file, not the whole folder"

    assert results[1].diff == "pkg.mod.mutant.1.diff", "diff names carry the module path"
    diff = (tmp_path / "out" / "survivors" / "pkg.mod.mutant.1.diff").read_text(encoding="utf-8")
    assert "-    return a - b" in diff
    assert "+    return a + b" in diff
    assert (tmp_path / "out" / "mutants" / "pkg.mod").is_dir(), "mutants keyed by module path"

    text = run.report()
    assert text.endswith("\n")
    assert "killed 2  survived 1  killed_compile 0  stillborn 1  equivalent 1" in text
    assert "kill rate: 66.7%" in text
    assert "SURVIVED  pkg/mod.py  mod.mutant.1.py  -> survivors/pkg.mod.mutant.1.diff" in text
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

    # a real plant, so there is something to restore: baseline green, then every run red
    with pytest.raises(MutationError, match="suite red after restoring"):
        MutationRun(tmp_path, tmp_path / "out", generate=fake_generate, run=red_after).run_source(
            src
        )
    assert src.read_text(encoding="utf-8") == SRC

    # a source the generator produces nothing for never runs a baseline: there is nothing to time,
    # and a suite that is red for its own reasons must not be blamed on a source with no mutants
    never = MutationRun(tmp_path, tmp_path / "out5", generate=lambda s, d: [], run=red)
    assert never.run_source(src) == []
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
    made: list[tuple[object, ...]] = []

    class FakeRun:
        def __init__(self, root, out, sample=0, seed=1, **hooks):
            self.args = (root, out, sample, seed)
            self.hooks = hooks
            made.append(self.args)

        def run_source(self, source):
            if source.name == "bad.py":
                raise MutationError("baseline is red for bad.py")
            return [
                mutation.Outcome("x", "m0", "KILLED", 0.1),
                mutation.Outcome("x", "m1", "SURVIVED", 0.1),
            ]

        def report(self):
            return "REPORT\n"

        def nothing_tested(self):
            return None

        def start(self):
            return ["alx/left.py"]

    monkeypatch.setattr(mutation, "MutationRun", FakeRun)
    argv = ["--root", str(tmp_path), "--out", "o", "--sample", "5", "alx/ok.py"]
    assert mutation.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("RECOVERED alx/left.py")
    assert "alx/ok.py: 1 killed, 0 killed_compile, 1 survived" in out
    assert out.endswith("REPORT\n")
    assert mutation.main(["--root", str(tmp_path), "alx/bad.py"]) == 1
    assert "MUTATION RUN FAILED: baseline is red for bad.py" in capsys.readouterr().out
    assert [m[2:] for m in made] == [(5, 1), (0, 1)], (
        "--sample and --seed reach the run; defaults 0 / 1"
    )


C_SRC = "int add(int a, int b) { return a + b; }\nint sub(int a, int b) { return a - b; }\n"
C_MUTANTS = {
    "alxMath.mutant.0.c": C_SRC.replace("a + b", "a - b"),  # killed by the tests
    "alxMath.mutant.1.c": C_SRC.replace("a - b", "a + b"),  # survives: sub is untested
    "alxMath.mutant.2.c": C_SRC.replace("return a + b;", "return a + b;  /* c */"),  # same object
    "alxMath.mutant.3.c": C_SRC.replace("a + b;", "a + b"),  # does not compile
    "alxMath.mutant.4.c": C_SRC.replace("a + b", "a * b"),  # the rebuild refuses this one
}


def c_project(root: Path) -> Path:
    src = root / "alxMath.c"
    src.write_text(C_SRC, encoding="utf-8")
    (root / "Test").mkdir()
    (root / "Test" / "test_alxMath.py").write_text("def test_add():\n    pass\n", encoding="utf-8")
    return src


def c_generate(source: Path, mutant_dir: Path) -> list[Path]:
    mutant_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, text in C_MUTANTS.items():
        (mutant_dir / name).write_text(text, encoding="utf-8")
        paths.append(mutant_dir / name)
    return paths


class CToolchain:
    """Scripted C toolchain: check = 'compiles', fingerprint = the code without comments, rebuild = not a * b."""

    def __init__(self, source: Path):
        self.source = source
        self.rebuilds = 0
        self.tests: list[str] = []

    def check(self, mutant: Path) -> bool:
        return mutant.read_text(encoding="utf-8").count(";") == 2

    def fingerprint(self, path: Path) -> str | None:
        text = path.read_text(encoding="utf-8")
        if text.count(";") != 2:
            return None
        return re.sub(r"/\*.*?\*/", "", text).replace(" ", "")

    def rebuild(self) -> bool:
        self.rebuilds += 1
        return "a * b" not in self.source.read_text(encoding="utf-8")

    def run_tests(self, cmd, cwd, capture_output, text, timeout, check, env):
        body = self.source.read_text(encoding="utf-8")
        self.tests.append(cmd[-1])
        rc = 0 if "return a + b" in body else 1
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")


def test_ALX1544_P134_language_hooks_check_fingerprint_rebuild(tmp_path):
    src = c_project(tmp_path)
    tc = CToolchain(src)
    run = MutationRun(
        tmp_path,
        tmp_path / "out",
        generate=c_generate,
        run=tc.run_tests,
        tests_dir="Test",
        check=tc.check,
        fingerprint_of=tc.fingerprint,
        rebuild=tc.rebuild,
    )
    results = run.run_source(src)
    assert [(r.mutant, r.status, r.detail) for r in results] == [
        ("alxMath.mutant.0.c", "KILLED", "rc=1"),
        ("alxMath.mutant.1.c", "SURVIVED", ""),
        ("alxMath.mutant.4.c", "KILLED_COMPILE", "rebuild"),
    ]
    assert run.counts() == {
        "KILLED": 1,
        "KILLED_COMPILE": 1,
        "SURVIVED": 1,
        "STILLBORN": 1,
        "EQUIVALENT": 1,
    }
    assert run.kill_rate() == 50.0, "compile kills are scored apart"
    assert tc.rebuilds == 4, "one per planted mutant + one after the restore"
    assert src.read_text(encoding="utf-8") == C_SRC
    assert "killed 1  survived 1  killed_compile 1  stillborn 1  equivalent 1" in run.report()

    class NoRebuild(CToolchain):
        def rebuild(self) -> bool:
            self.rebuilds += 1
            return self.rebuilds < 3  # fails on the restore

    broken = NoRebuild(src)
    with pytest.raises(MutationError, match="rebuild failed after restoring"):
        MutationRun(
            tmp_path,
            tmp_path / "out2",
            generate=c_generate,
            run=broken.run_tests,
            tests_dir="Test",
            check=broken.check,
            fingerprint_of=broken.fingerprint,
            rebuild=broken.rebuild,
        ).run_source(src)
    assert src.read_text(encoding="utf-8") == C_SRC


def test_ALX1544_P135_root_level_c_source_with_a_test_folder(tmp_path):
    src = c_project(tmp_path)
    tc = CToolchain(src)
    run = MutationRun(
        tmp_path,
        tmp_path / "out",
        generate=c_generate,
        run=tc.run_tests,
        tests_dir="Test",
        check=tc.check,
        fingerprint_of=tc.fingerprint,
    )
    assert (
        mutation.default_tests_for(tmp_path, src, "Test") == tmp_path / "Test" / "test_alxMath.py"
    )
    assert mutation.default_tests_for(tmp_path, src) == tmp_path / "tests", (
        "no tests/ folder: fallback"
    )
    results = run.run_source(src)
    assert tc.tests[0].endswith("test_alxMath.py")
    assert (tmp_path / "out" / "mutants" / "alxMath").is_dir(), "key = stem without the .c suffix"
    survivor = next(r for r in results if r.status == "SURVIVED")
    assert survivor.diff == "alxMath.mutant.1.diff"
    assert (tmp_path / "out" / "survivors" / "alxMath.mutant.1.diff").exists()


def test_ALX1544_P136_command_hooks_and_main_wiring(tmp_path, monkeypatch, capsys):
    good = tmp_path / "good.c"
    good.write_text("ok", encoding="utf-8")
    py = Path(sys.executable).as_posix()  # hook templates take POSIX-style paths
    check = mutation.check_command(
        py
        + ' -c "import sys; sys.exit(0 if open(sys.argv[1]).read() == chr(111)+chr(107) else 1)" {mutant}',
        tmp_path,
    )
    assert check(good) is True
    bad = tmp_path / "bad.c"
    bad.write_text("no", encoding="utf-8")
    assert check(bad) is False
    fp = mutation.fingerprint_command(
        py + ' -c "import sys; print(len(open(sys.argv[1]).read()))" {mutant}', tmp_path
    )
    assert fp(good) == "2"
    assert fp(bad) == "2", "same length, same fingerprint: EQUIVALENT by this hook"
    failing = mutation.fingerprint_command(py + ' -c "import sys; sys.exit(3)" {source}', tmp_path)
    assert failing(good) is None
    rebuild_ok = mutation.rebuild_command(py + ' -c "import sys; sys.exit(0)"', tmp_path)
    rebuild_bad = mutation.rebuild_command(py + ' -c "import sys; sys.exit(2)"', tmp_path)
    assert rebuild_ok() is True
    assert rebuild_bad() is False

    calls: list[dict[str, object]] = []

    class FakeRun:
        def __init__(self, root, out, sample=0, seed=1, **hooks):
            calls.append(dict(hooks))

        def start(self):
            return []

        def run_source(self, source):
            return []

        def report(self):
            return "REPORT\n"

        def nothing_tested(self):
            return None

    monkeypatch.setattr(mutation, "MutationRun", FakeRun)
    argv = [
        "--root", str(tmp_path), "--tests-dir", "Test",
        "--check-cmd", "c {mutant}", "--fingerprint-cmd", "f {mutant}", "--rebuild-cmd", "b",
        "alxMath.c",
    ]  # fmt: skip
    assert mutation.main(argv) == 0
    assert mutation.main(["--root", str(tmp_path), "alxMath.c"]) == 0
    with_hooks, defaults = calls
    assert with_hooks["tests_dir"] == "Test"
    assert callable(with_hooks["check"])
    assert callable(with_hooks["rebuild"])
    assert with_hooks["fingerprint_of"] is not mutation.fingerprint_file
    assert defaults["tests_dir"] == "tests"
    assert defaults["check"] is None
    assert defaults["rebuild"] is None
    assert defaults["fingerprint_of"] is mutation.fingerprint_file
    assert capsys.readouterr().out.count("REPORT") == 2


def test_ALX1544_P182_a_run_that_tested_nothing_says_so_and_fails(tmp_path):
    """A broken check hook files every mutant STILLBORN; that is not a pass, it is no measurement."""
    src = project(tmp_path)
    out = tmp_path / "out"

    # every mutant rejected by the check hook = what a wrong compiler path looks like from here
    run = MutationRun(
        tmp_path, out, generate=fake_generate, run=ScriptedRunner(src), check=lambda _m: False
    )
    run.run_source(src)
    counts = run.counts()
    assert counts["STILLBORN"] == sum(counts.values()) > 0, "everything was filed stillborn"
    assert run.kill_rate() is None, "the old report said only this, and exited 0"

    reason = run.nothing_tested()
    assert reason is not None
    assert "none reached the tests" in reason
    assert "the check hook is the suspect, not the suite" in reason
    assert "NOTHING TESTED:" in run.report()

    # one mutant that does reach the tests is enough to make the run a measurement again
    healthy = MutationRun(
        tmp_path, tmp_path / "out2", generate=fake_generate, run=ScriptedRunner(src)
    )
    healthy.run_source(src)
    assert healthy.nothing_tested() is None
    assert "NOTHING TESTED" not in healthy.report()

    # a source the generator produced nothing for is not a broken hook, so it is not flagged
    empty = MutationRun(
        tmp_path, tmp_path / "out3", generate=lambda s, d: [], run=ScriptedRunner(src)
    )
    empty.run_source(src)
    assert empty.nothing_tested() is None


def test_ALX1544_P186_statements_that_do_nothing_are_dropped_from_the_fingerprint():
    """A third of a real survivor list was mutants of shapes that cannot change what code does.

    Measured on the first full run of this package: universalmutator replaces a docstring with
    `pass` and appends `continue` to loop bodies. Neither changes behaviour, so neither should
    reach a list a human has to read. What must NOT be dropped is the near neighbour of each: a
    `break` in the same place, and a `continue` that is not last.
    """
    fp = mutation.fingerprint

    # dropped: they execute nothing
    assert fp('def f(a):\n    """Doc."""\n    return a\n', "m.py") == fp(
        "def f(a):\n    pass\n    return a\n", "m.py"
    )
    assert fp("class P:\n    def m(self):\n        ...\n", "m.py") == fp(
        "class P:\n    def m(self):\n        pass\n", "m.py"
    )
    assert fp("for x in y:\n    z(x)\n", "m.py") == fp(
        "for x in y:\n    z(x)\n    continue\n", "m.py"
    )
    assert fp("while c:\n    z()\n", "m.py") == fp("while c:\n    z()\n    continue\n", "m.py")

    # kept: each of these DOES change what the code does
    assert fp("for x in y:\n    z(x)\n", "m.py") != fp(
        "for x in y:\n    z(x)\n    break\n", "m.py"
    ), "a break stops the loop"
    assert fp("for x in y:\n    z(x)\n    w(x)\n", "m.py") != fp(
        "for x in y:\n    z(x)\n    continue\n    w(x)\n", "m.py"
    ), "a continue that is not last skips the rest"
    assert fp("def f(a, b):\n    return a + b\n", "m.py") != fp(
        "def f(a, b):\n    return a - b\n", "m.py"
    )
    assert fp("def f():\n    return 1\n", "m.py") != fp("def f():\n    pass\n", "m.py"), (
        "deleting a return is not deleting nothing"
    )


def test_ALX1544_P188_a_continue_in_tail_position_of_a_loop_is_dropped():
    """Most appended `continue` mutants end a nested block, not the loop body itself.

    Control was going to the top of the iteration either way, so they are equivalent - but only
    where that is certain. A nested loop owns its own `continue`, and a `try` can have a `finally`
    and handlers, so neither is followed.
    """
    fp = mutation.fingerprint

    # dropped: tail position of the loop, however deeply nested inside if and with blocks
    assert fp("for x in y:\n    if c:\n        z(x)\n", "m.py") == fp(
        "for x in y:\n    if c:\n        z(x)\n        continue\n", "m.py"
    )
    assert fp("for x in y:\n    if c:\n        a()\n    else:\n        b()\n", "m.py") == fp(
        "for x in y:\n    if c:\n        a()\n    else:\n        b()\n        continue\n", "m.py"
    )
    assert fp("for x in y:\n    with open(x) as f:\n        z(f)\n", "m.py") == fp(
        "for x in y:\n    with open(x) as f:\n        z(f)\n        continue\n", "m.py"
    )
    assert fp("while c:\n    if a:\n        if b:\n            z()\n", "m.py") == fp(
        "while c:\n    if a:\n        if b:\n            z()\n            continue\n", "m.py"
    )

    # kept: not tail position, or not certain
    assert fp("for x in y:\n    if c:\n        z(x)\n    w(x)\n", "m.py") != fp(
        "for x in y:\n    if c:\n        z(x)\n        continue\n    w(x)\n", "m.py"
    ), "something still runs after it"
    assert fp("for x in y:\n    try:\n        z(x)\n    finally:\n        c()\n", "m.py") != fp(
        "for x in y:\n    try:\n        z(x)\n        continue\n    finally:\n        c()\n", "m.py"
    ), "a try is not followed: finally and the handlers make it a real question"
    assert fp("for x in y:\n    if c:\n        z(x)\n", "m.py") != fp(
        "for x in y:\n    if c:\n        z(x)\n        break\n", "m.py"
    ), "a break in the same slot stops the loop"


class CountingGenerator:
    """fake_generate, but it records how often it actually ran."""

    def __init__(self):
        self.calls = 0

    def __call__(self, source: Path, mutant_dir: Path) -> list[Path]:
        self.calls += 1
        return fake_generate(source, mutant_dir)


def test_ALX1544_P189_the_mutant_pool_is_generated_and_filtered_once_per_source(tmp_path):
    """Generating and filtering is nearly all of a C run and none of it depends on the tests.

    One C source produced 911 mutants and about 1400 compiler calls to decide which were
    stillborn or equivalent - nine minutes to end up testing three. That work depends only on the
    source, so it is cached; what must NOT survive is a cache made from different bytes or by
    different hooks.
    """
    src = project(tmp_path)
    out = tmp_path / "out"
    generate = CountingGenerator()

    first = MutationRun(tmp_path, out, generate=generate, run=ScriptedRunner(src))
    first.run_source(src)
    assert generate.calls == 1

    second = MutationRun(tmp_path, out, generate=generate, run=ScriptedRunner(src))
    second.run_source(src)
    assert generate.calls == 1, "the pool was reused"
    assert second.counts() == first.counts(), (
        "a cached run reports the same STILLBORN and EQUIVALENT as the run that filled the cache"
    )

    # a different pool_id means different hooks decided it, so it is not the same pool
    other = MutationRun(tmp_path, out, generate=generate, run=ScriptedRunner(src), pool_id="clang")
    other.run_source(src)
    assert generate.calls == 2, "a pool filtered by other hooks is not reused"
    assert other.counts() == first.counts()

    # editing the source invalidates it, the moment it matters and not before
    src.write_text(SRC.replace("a - b", "b - a"), encoding="utf-8")
    edited = MutationRun(tmp_path, out, generate=generate, run=ScriptedRunner(src), pool_id="clang")
    edited.run_source(src)
    assert generate.calls == 3, "the digest is the source's own bytes"

    # and the cache can be refused outright
    off = MutationRun(tmp_path, out, generate=generate, run=ScriptedRunner(src), pool=False)
    off.run_source(src)
    off.run_source(src)
    assert generate.calls == 5, "pool=False generates every time"
