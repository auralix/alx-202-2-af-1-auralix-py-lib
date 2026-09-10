# SPDX-License-Identifier: MIT
"""MUTATE lane: plant each mutant of a source, run its tests, classify, report (tests the tests).

The same procedure as the C library's mutation run, with the same generator (universalmutator) and
without a compile step: for every mutant of a source file

* ``STILLBORN``: the mutant does not even compile; dropped before any test
* ``EQUIVALENT``: the mutant has the same normalized AST as the original - docstrings, type
  annotations and source positions ignored (a whitespace, comment, docstring or annotation change);
  dropped, nothing that runs has changed
* ``KILLED_COMPILE``: the rebuild after planting failed (a compiled language's strict build caught
  the mutant before any test ran; scored separately)
* ``KILLED``: the tests went red or hung (timeout) - the tests noticed the planted bug
* ``SURVIVED``: the tests stayed green - a hole in the tests, or a behaviourally equivalent mutant;
  judge by the diff written to ``<out>/survivors/``

Kill rate = killed / (killed + survived). Report-only: exit code 0 unless the runner itself fails
(red baseline, source not restored). 100 % is not the target; the survivor diffs are the output.
Usage::

    python -m alx.verify.mutation [--out build/mutate] [--sample 100] [--seed 1] src.py ...
    python -m alx.verify.mutation --tests-dir Test --rebuild-cmd "<build the test DLL>" \
        --check-cmd "clang -fsyntax-only {mutant}" --fingerprint-cmd "<hash of {mutant}>" alxFoo.c

Other languages plug in three commands (``{mutant}`` and ``{source}`` are replaced): ``--check-cmd``
(non-zero exit = STILLBORN), ``--fingerprint-cmd`` (prints a fingerprint; equal to the original's =
EQUIVALENT, the C library compares object files), ``--rebuild-cmd`` (runs after planting and after
the restore; non-zero exit = KILLED_COMPILE). Python needs none: the normalized AST is the
fingerprint and a parse error the check.

The tests of a source default to its mirror in the tests folder (``--tests-dir``, default
``tests``): ``alx/pkg/mod.py`` maps to ``tests/pkg/test_mod.py``, ``alx/pkg/__init__.py`` to
``tests/pkg/test_pkg.py``, ``alx/mod.py`` and a root-level ``mod.c`` to ``tests/test_mod.py``;
without a mirror the whole tests folder runs. Each run is
``python -m pytest -q -x`` without random ordering and without the cache; bytecode caches are
disabled while mutants are planted. The original of a planted source is kept under
``<out>/backup/`` until it is restored, so a run killed mid-plant (a reboot) is repaired by the
next start, never left in the tree.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import functools
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

STATUSES = ("KILLED", "KILLED_COMPILE", "SURVIVED", "STILLBORN", "EQUIVALENT")

Runner = Callable[..., subprocess.CompletedProcess[str]]
Generator = Callable[[Path, Path], list[Path]]


@dataclass(frozen=True)
class Outcome:
    """One mutant's verdict."""

    source: str
    mutant: str
    status: str  # one of STATUSES
    seconds: float
    detail: str = ""
    diff: str = ""  # survivors: the diff file under <out>/survivors/


class MutationError(RuntimeError):
    """The runner itself failed (red baseline, generator missing, source not restored)."""


def universalmutator(source: Path, mutant_dir: Path) -> list[Path]:
    """Generate the mutants of ``source`` into ``mutant_dir`` (universalmutator's ``mutate``)."""
    exe = shutil.which("mutate") or str(Path(sys.executable).with_name("mutate"))
    mutant_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell; exe is resolved above
            [exe, str(source), "--mutantDir", str(mutant_dir), "--noCheck"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as ex:
        raise MutationError(f"mutate not found ({exe}): install universalmutator - {ex}") from ex
    if result.returncode != 0:
        raise MutationError(
            f"mutate failed for {source}:\n{result.stdout[-800:]}\n{result.stderr[-800:]}"
        )
    mutants = mutant_dir.glob(f"{source.stem}.mutant.*{source.suffix}")
    return sorted(mutants, key=lambda p: int(p.suffixes[-2][1:]))


class _Normalize(ast.NodeTransformer):
    """Drop what never runs: docstrings, type annotations (the positions go with ``ast.dump``)."""

    @staticmethod
    def _strip_doc(
        node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        body = node.body
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body = body[1:]
        node.body = body or [ast.Pass()]

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self.generic_visit(node)
        self._strip_doc(node)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self.generic_visit(node)
        self._strip_doc(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                arg.annotation = None
        self._strip_doc(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                arg.annotation = None
        self._strip_doc(node)
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if node.value is None:
            return None  # a bare declaration runs nothing: dropped
        return ast.Assign(targets=[node.target], value=node.value)


def fingerprint(text: str, filename: str) -> str | None:
    """Return the normalized AST of ``text`` as text; None when it does not parse.

    Docstrings, type annotations and source positions are dropped, so a mutant that only edits a
    docstring or an annotation, or that inserts a line inside one, is EQUIVALENT, never a survivor.
    """
    try:
        tree = ast.parse(text, filename)
    except (SyntaxError, ValueError):
        return None
    return ast.dump(_Normalize().visit(tree))


def fingerprint_file(path: Path) -> str | None:
    """Return the normalized AST of a Python file (the default hook); None if it does not parse."""
    return fingerprint(path.read_text(encoding="utf-8", errors="replace"), str(path))


def command(template: str, root: Path, **fields: Path) -> subprocess.CompletedProcess[str]:
    """Run a hook command template in ``root``; ``{mutant}`` and ``{source}`` are POSIX paths."""
    argv = shlex.split(template.format(**{k: v.as_posix() for k, v in fields.items()}))
    # the template comes from the lane that started the run (a noxfile), never from mutated content
    return subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)  # noqa: S603


def check_command(template: str, root: Path) -> Callable[[Path], bool]:
    """Turn ``--check-cmd`` into the check hook: exit code 0 = the mutant is viable."""
    return lambda mutant: command(template, root, mutant=mutant).returncode == 0


def fingerprint_command(template: str, root: Path) -> Callable[[Path], str | None]:
    """Turn ``--fingerprint-cmd`` into the fingerprint hook: stdout is the value, failure = None."""

    def hook(path: Path) -> str | None:
        result = command(template, root, mutant=path, source=path)
        return result.stdout.strip() if result.returncode == 0 else None

    return hook


def rebuild_command(template: str, root: Path) -> Callable[[], bool]:
    """Turn ``--rebuild-cmd`` into the rebuild hook: exit code 0 = built."""
    return lambda: command(template, root).returncode == 0


def default_tests_for(root: Path, source: Path, tests_dir: str = "tests") -> Path:
    """Return the test file mirroring ``source`` under ``root/<tests_dir>``, or that folder."""
    rel = source.resolve().relative_to(root.resolve())
    tests = root / tests_dir
    inner = Path(*rel.parts[1:]) if len(rel.parts) > 1 else rel  # drop the package root folder
    if inner.name == "__init__.py" and inner.parent == Path():
        candidate = tests / f"test_{rel.parts[0]}.py"  # the root package itself
    elif inner.name == "__init__.py":
        candidate = tests / inner.parent / f"test_{inner.parent.name}.py"
    else:
        candidate = tests / inner.parent / f"test_{inner.stem}.py"
    return candidate if candidate.exists() else tests


class MutationRun:
    """Plant mutants of the given sources one by one, run their tests, restore, report."""

    def __init__(
        self,
        root: str | Path,
        out: str | Path,
        sample: int = 0,
        seed: int = 1,
        timeout_factor: float = 5.0,
        min_timeout_s: float = 20.0,
        generate: Generator = universalmutator,
        run: Runner = subprocess.run,
        tests_for: Callable[[Path, Path], Path] | None = None,
        tests_dir: str = "tests",
        check: Callable[[Path], bool] | None = None,
        fingerprint_of: Callable[[Path], str | None] = fingerprint_file,
        rebuild: Callable[[], bool] | None = None,
    ):
        """Configure the run: repository, output folder, sampling, and the language hooks.

        ``generate``, ``run``, ``check``, ``fingerprint_of`` and ``rebuild`` are the seams: the
        defaults are the Python recipe, a compiled language passes its own (the C library does).
        """
        self.root = Path(root).resolve()
        self.out = Path(out)
        self.sample = sample
        self.seed = seed
        self.timeout_factor = timeout_factor
        self.min_timeout_s = min_timeout_s
        self._generate = generate
        self._run = run
        self._tests_for = tests_for or functools.partial(default_tests_for, tests_dir=tests_dir)
        self._check = check
        self._fingerprint = fingerprint_of
        self._rebuild = rebuild
        self.outcomes: list[Outcome] = []

    # -- pieces ---------------------------------------------------------------------------
    def test_command(self, source: Path) -> list[str]:
        """Return the pytest command for one source: its mirror test file or the tests folder."""
        tests = self._tests_for(self.root, source)
        return [
            sys.executable, "-m", "pytest", "-q", "-x",
            "-p", "no:randomly", "-p", "no:cacheprovider",
            "-o", "addopts=", str(tests),
        ]  # fmt: skip

    def _pytest(self, source: Path, timeout_s: float) -> tuple[int, float]:
        """Run the tests of ``source``; return ``(returncode, seconds)``, -1 on timeout."""
        rc, seconds, _ = self._pytest_out(source, timeout_s)
        return rc, seconds

    def _pytest_out(self, source: Path, timeout_s: float) -> tuple[int, float, str]:
        """As ``_pytest``, and the tests' own output - the only thing that explains a red run."""
        t0 = time.monotonic()
        try:
            result = self._run(
                self.test_command(source),
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return -1, time.monotonic() - t0, "timed out"
        return (
            result.returncode,
            time.monotonic() - t0,
            (result.stdout or "") + (result.stderr or ""),
        )

    def baseline(self, source: Path) -> float:
        """Run the tests on the untouched source; return the duration, raise when they are red.

        The tests' own output goes into the error. Without it the message is "rc=1" and the reason
        is gone, which is no use at three in the morning - and a baseline can be red for reasons
        that have nothing to do with the source being mutated, a flaky timing test under the load
        of the run itself among them.
        """
        rc, seconds, output = self._pytest_out(source, timeout_s=600.0)
        if rc != 0:
            tail = "\n".join(output.strip().splitlines()[-25:])
            raise MutationError(
                f"baseline is red for {source} (rc={rc}); fix the suite before mutating\n{tail}"
            )
        return seconds

    def viable(self, source: Path, mutants: list[Path]) -> list[Path]:
        """Drop STILLBORN (check fails, no parse) and EQUIVALENT (same fingerprint) mutants."""
        original = self._fingerprint(source)
        keep = []
        for mutant in mutants:
            if self._check is not None and not self._check(mutant):
                self.outcomes.append(Outcome(str(source), mutant.name, "STILLBORN", 0.0, "check"))
                continue
            code = self._fingerprint(mutant)
            if code is None:
                self.outcomes.append(Outcome(str(source), mutant.name, "STILLBORN", 0.0))
            elif code == original:
                self.outcomes.append(Outcome(str(source), mutant.name, "EQUIVALENT", 0.0))
            else:
                keep.append(mutant)
        return keep

    # -- crash safety ---------------------------------------------------------------------
    def recover(self) -> list[str]:
        """Restore every source a crashed run left planted (its copy under ``out/backup``)."""
        backup = self.out / "backup"
        restored = []
        for copy in sorted(p for p in backup.rglob("*") if p.is_file()):
            rel = copy.relative_to(backup).as_posix()
            target = self.root / rel
            if not target.exists() or target.read_bytes() != copy.read_bytes():
                target.write_bytes(copy.read_bytes())
                restored.append(rel)
        shutil.rmtree(backup, ignore_errors=True)
        return restored

    def start(self) -> list[str]:
        """Begin a run: recover a crashed one, then clear the previous run's mutants and results."""
        restored = self.recover()
        for stale in ("mutants", "survivors"):
            shutil.rmtree(self.out / stale, ignore_errors=True)
        for stale_file in ("report.txt", "results.json"):
            (self.out / stale_file).unlink(missing_ok=True)
        return restored

    def run_source(self, source: Path) -> list[Outcome]:
        """Baseline, generate, filter, sample, plant and test each mutant, restore, verify."""
        source = source.resolve()
        rel = source.relative_to(self.root).as_posix()
        key = rel.removesuffix(source.suffix).replace("/", ".")  # alx/c_lib/cli.py -> alx.c_lib.cli
        self.recover()
        self._clear_pycache(rel)
        mutants = self.viable(source, self._generate(source, self.out / "mutants" / key))
        if not mutants:
            return []  # nothing to plant, so nothing to time: a baseline run would be pure cost
        base_s = self.baseline(source)
        timeout_s = max(self.min_timeout_s, self.timeout_factor * base_s)
        if self.sample and len(mutants) > self.sample:
            picked = random.Random(self.seed).sample(mutants, self.sample)  # noqa: S311
            mutants = sorted(picked)
        original = source.read_bytes()
        backup = self.out / "backup" / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)  # survives a crash; recover() puts it back next time
        results: list[Outcome] = []
        try:
            for mutant in mutants:
                source.write_bytes(mutant.read_bytes())
                t0 = time.monotonic()
                if self._rebuild is not None and not self._rebuild():
                    seconds = time.monotonic() - t0
                    results.append(
                        Outcome(rel, mutant.name, "KILLED_COMPILE", round(seconds, 2), "rebuild")
                    )
                    continue
                rc, seconds = self._pytest(source, timeout_s)
                diff = ""
                if rc == 0:
                    status, detail = "SURVIVED", ""
                    diff = self._write_diff(original, mutant, source, key)
                elif rc == -1:
                    status, detail = "KILLED", "timeout"
                else:
                    status, detail = "KILLED", f"rc={rc}"
                results.append(Outcome(rel, mutant.name, status, round(seconds, 2), detail, diff))
        finally:
            source.write_bytes(original)
            self._clear_pycache(rel)
        if source.read_bytes() != original:  # pragma: no cover - a write that did not stick
            raise MutationError(f"{source} not restored")
        shutil.rmtree(self.out / "backup", ignore_errors=True)  # restored: nothing to recover
        if self._rebuild is not None and not self._rebuild():
            raise MutationError(f"rebuild failed after restoring {source}")
        rc, _ = self._pytest(source, timeout_s=600.0)
        if rc != 0:
            raise MutationError(f"suite red after restoring {source} (rc={rc})")
        self.outcomes.extend(results)
        return results

    def _clear_pycache(self, rel: str) -> None:
        top = self.root / rel.split("/", maxsplit=1)[0]
        if top.is_dir():
            clear_pycache(top)

    def _write_diff(self, original: bytes, mutant: Path, source: Path, key: str) -> str:
        """Write the survivor's diff as ``<key>.mutant.<n>.diff``; return that file name."""
        survivors = self.out / "survivors"
        survivors.mkdir(parents=True, exist_ok=True)
        diff = difflib.unified_diff(
            original.decode("utf-8", "replace").splitlines(),  # line endings do not count
            mutant.read_text(encoding="utf-8", errors="replace").splitlines(),
            fromfile=source.name,
            tofile=mutant.name,
            lineterm="",
        )
        name = f"{key}{mutant.stem.removeprefix(source.stem)}.diff"
        (survivors / name).write_text("\n".join(diff) + "\n", encoding="utf-8")
        return name

    # -- report ---------------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        """Return ``{status: count}`` over every outcome so far."""
        counts = dict.fromkeys(STATUSES, 0)
        for outcome in self.outcomes:
            counts[outcome.status] += 1
        return counts

    def kill_rate(self) -> float | None:
        """Return killed / (killed + survived) in percent, None when nothing was run."""
        counts = self.counts()
        tested = counts["KILLED"] + counts["SURVIVED"]
        return None if tested == 0 else 100.0 * counts["KILLED"] / tested

    def nothing_tested(self) -> str | None:
        """Return why this run proves nothing, or None when a mutant did reach the tests.

        A run where the generator produced mutants and NOT ONE of them ever reached the suite has
        not measured the suite - it has measured a broken hook. That is exactly what a wrong
        compiler path looks like from in here: every mutant fails its syntax check, every one is
        filed STILLBORN, and the report reads "kill rate: n/a" beside a four-figure count. Seen for
        real on 10.09 in the C library, and a lane that tested nothing must not be able to pass.
        """
        counts = self.counts()
        if counts["KILLED"] + counts["SURVIVED"] + counts["KILLED_COMPILE"] > 0:
            return None
        generated = sum(counts.values())
        if generated == 0:
            return None  # nothing was generated either: an empty source, not a broken hook
        blamed = "check" if counts["STILLBORN"] == generated else "check / fingerprint"
        return (
            f"{generated} mutants generated and none reached the tests "
            f"({counts['STILLBORN']} stillborn, {counts['EQUIVALENT']} equivalent) - "
            f"the {blamed} hook is the suspect, not the suite"
        )

    def report(self) -> str:
        """Write ``report.txt`` + ``results.json`` under ``out`` and return the report text."""
        counts = self.counts()
        rate = self.kill_rate()
        lines = [
            "MUTATION RUN (report-only)",
            f"killed {counts['KILLED']}  survived {counts['SURVIVED']}  "
            f"killed_compile {counts['KILLED_COMPILE']}  "
            f"stillborn {counts['STILLBORN']}  equivalent {counts['EQUIVALENT']}",
            "kill rate: " + ("n/a" if rate is None else f"{rate:.1f}%"),
            "",
        ]
        broken = self.nothing_tested()
        if broken is not None:
            lines.insert(1, f"NOTHING TESTED: {broken}")
        lines.extend(
            f"SURVIVED  {o.source}  {o.mutant}  -> survivors/{o.diff}"
            for o in self.outcomes
            if o.status == "SURVIVED"
        )
        text = "\n".join(lines) + "\n"
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "report.txt").write_text(text, encoding="utf-8")
        (self.out / "results.json").write_text(
            json.dumps([asdict(o) for o in self.outcomes], indent=1), encoding="utf-8"
        )
        return text


def clear_pycache(package_dir: Path) -> None:
    """Remove every ``__pycache__`` under ``package_dir`` (no stale .pyc for a planted mutant)."""
    for cache in package_dir.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.mutation", description=__doc__)
    parser.add_argument(
        "sources", nargs="+", help="source files to mutate (relative to the repo root)"
    )
    parser.add_argument("--root", default=".", help="repository root (default: current folder)")
    parser.add_argument("--out", default="build/mutate", help="output folder (the lane's evidence)")
    parser.add_argument("--sample", type=int, default=0, help="mutants per source, 0 = all")
    parser.add_argument("--seed", type=int, default=1, help="sampling seed")
    parser.add_argument(
        "--tests-dir", default="tests", help="tests folder under root (default tests)"
    )
    parser.add_argument(
        "--check-cmd", help="viability check of {mutant}; non-zero exit = STILLBORN"
    )
    parser.add_argument(
        "--fingerprint-cmd",
        help="prints a fingerprint of {mutant}; equal to the original's = EQUIVALENT",
    )
    parser.add_argument(
        "--rebuild-cmd",
        help="runs after planting and after the restore; non-zero exit = KILLED_COMPILE",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    run = MutationRun(
        args.root,
        args.out,
        sample=args.sample,
        seed=args.seed,
        tests_dir=args.tests_dir,
        check=check_command(args.check_cmd, root) if args.check_cmd else None,
        fingerprint_of=(
            fingerprint_command(args.fingerprint_cmd, root)
            if args.fingerprint_cmd
            else fingerprint_file
        ),
        rebuild=rebuild_command(args.rebuild_cmd, root) if args.rebuild_cmd else None,
    )
    try:
        for rel in run.start():
            sys.stdout.write(f"RECOVERED {rel} (a previous run was interrupted while planted)\n")
        for src in args.sources:
            results = run.run_source(Path(args.root) / src)
            killed = sum(r.status == "KILLED" for r in results)
            compile_killed = sum(r.status == "KILLED_COMPILE" for r in results)
            survived = len(results) - killed - compile_killed
            sys.stdout.write(
                f"{src}: {killed} killed, {compile_killed} killed_compile, {survived} survived\n"
            )
    except MutationError as ex:
        sys.stdout.write(f"MUTATION RUN FAILED: {ex}\n")
        return 1
    sys.stdout.write(run.report())
    return 0 if run.nothing_tested() is None else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
