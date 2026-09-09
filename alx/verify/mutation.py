# SPDX-License-Identifier: MIT
"""MUTATE lane: plant each mutant of a source, run its tests, classify, report (tests the tests).

The same procedure as the C library's mutation run, with the same generator (universalmutator) and
without a compile step: for every mutant of a source file

* ``STILLBORN``: the mutant does not even compile (``py_compile``); dropped before any test
* ``EQUIVALENT``: the mutant compiles to the very same bytecode as the original (a whitespace or
  comment change); dropped, nothing the interpreter sees has changed
* ``KILLED``: the tests went red or hung (timeout) - the tests noticed the planted bug
* ``SURVIVED``: the tests stayed green - a hole in the tests, or a behaviourally equivalent mutant;
  judge by the diff written to ``<out>/survivors/``

Kill rate = killed / (killed + survived). Report-only: exit code 0 unless the runner itself fails
(red baseline, source not restored). 100 % is not the target; the survivor diffs are the output.
Usage::

    python -m alx.verify.mutation [--out build/mutation] [--sample 100] [--seed 1] src.py ...

The tests of a source default to its mirror in ``tests/``: ``alx/pkg/mod.py`` maps to
``tests/pkg/test_mod.py``, ``alx/pkg/__init__.py`` to ``tests/pkg/test_pkg.py`` and ``alx/mod.py``
to ``tests/test_mod.py``; without a mirror the whole ``tests/`` folder runs. Each run is
``python -m pytest -q -x`` without random ordering and without the cache; bytecode caches are
disabled while mutants are planted.
"""

from __future__ import annotations

import argparse
import difflib
import json
import marshal
import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]
Generator = Callable[[Path, Path], list[Path]]


@dataclass(frozen=True)
class Outcome:
    """One mutant's verdict."""

    source: str
    mutant: str
    status: str  # KILLED | SURVIVED | STILLBORN | EQUIVALENT
    seconds: float
    detail: str = ""


class MutationError(RuntimeError):
    """The runner itself failed (red baseline, generator missing, source not restored)."""


def universalmutator(source: Path, mutant_dir: Path) -> list[Path]:
    """Generate the mutants of ``source`` into ``mutant_dir`` (universalmutator's ``mutate``)."""
    exe = shutil.which("mutate") or str(Path(sys.executable).with_name("mutate"))
    mutant_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
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
    mutants = mutant_dir.glob(f"{source.stem}.mutant.*.py")
    return sorted(mutants, key=lambda p: int(p.suffixes[-2][1:]))


def bytecode(text: str, filename: str) -> bytes | None:
    """Return the marshalled code object of ``text``; None when it does not compile."""
    try:
        return marshal.dumps(compile(text, filename, "exec"))
    except (SyntaxError, ValueError):
        return None


def default_tests_for(root: Path, source: Path) -> Path:
    """Return the test file mirroring ``source`` under ``root/tests``, or the ``tests`` folder."""
    rel = source.resolve().relative_to(root.resolve())
    inner = Path(*rel.parts[1:])  # drop the package root folder (alx/)
    if inner.name == "__init__.py":
        candidate = root / "tests" / inner.parent / f"test_{inner.parent.name}.py"
    else:
        candidate = root / "tests" / inner.parent / f"test_{inner.stem}.py"
    return candidate if candidate.exists() else root / "tests"


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
        tests_for: Callable[[Path, Path], Path] = default_tests_for,
    ):
        self.root = Path(root).resolve()
        self.out = Path(out)
        self.sample = sample
        self.seed = seed
        self.timeout_factor = timeout_factor
        self.min_timeout_s = min_timeout_s
        self._generate = generate
        self._run = run
        self._tests_for = tests_for
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
            return -1, time.monotonic() - t0
        return result.returncode, time.monotonic() - t0

    def baseline(self, source: Path) -> float:
        """Run the tests on the untouched source; return the duration, raise when they are red."""
        rc, seconds = self._pytest(source, timeout_s=600.0)
        if rc != 0:
            raise MutationError(
                f"baseline is red for {source} (rc={rc}); fix the suite before mutating"
            )
        return seconds

    def viable(self, source: Path, mutants: list[Path]) -> list[Path]:
        """Drop STILLBORN (no compile) and EQUIVALENT (same bytecode) mutants, recording them."""
        original = bytecode(source.read_text(encoding="utf-8"), str(source))
        keep = []
        for mutant in mutants:
            code = bytecode(mutant.read_text(encoding="utf-8", errors="replace"), str(source))
            if code is None:
                self.outcomes.append(Outcome(str(source), mutant.name, "STILLBORN", 0.0))
            elif code == original:
                self.outcomes.append(Outcome(str(source), mutant.name, "EQUIVALENT", 0.0))
            else:
                keep.append(mutant)
        return keep

    def run_source(self, source: Path) -> list[Outcome]:
        """Baseline, generate, filter, sample, plant and test each mutant, restore, verify."""
        source = source.resolve()
        rel = source.relative_to(self.root).as_posix()
        clear_pycache(self.root / rel.split("/")[0])
        base_s = self.baseline(source)
        timeout_s = max(self.min_timeout_s, self.timeout_factor * base_s)
        mutants = self.viable(source, self._generate(source, self.out / "mutants" / source.stem))
        if self.sample and len(mutants) > self.sample:
            picked = random.Random(self.seed).sample(mutants, self.sample)  # noqa: S311
            mutants = sorted(picked)
        original = source.read_bytes()
        results: list[Outcome] = []
        try:
            for mutant in mutants:
                source.write_bytes(mutant.read_bytes())
                rc, seconds = self._pytest(source, timeout_s)
                if rc == 0:
                    status, detail = "SURVIVED", ""
                    self._write_diff(original, mutant, source)
                elif rc == -1:
                    status, detail = "KILLED", "timeout"
                else:
                    status, detail = "KILLED", f"rc={rc}"
                results.append(Outcome(rel, mutant.name, status, round(seconds, 2), detail))
        finally:
            source.write_bytes(original)
            clear_pycache(self.root / rel.split("/")[0])
        if source.read_bytes() != original:  # pragma: no cover - a write that did not stick
            raise MutationError(f"{source} not restored")
        rc, _ = self._pytest(source, timeout_s=600.0)
        if rc != 0:
            raise MutationError(f"suite red after restoring {source} (rc={rc})")
        self.outcomes.extend(results)
        return results

    def _write_diff(self, original: bytes, mutant: Path, source: Path) -> None:
        survivors = self.out / "survivors"
        survivors.mkdir(parents=True, exist_ok=True)
        diff = difflib.unified_diff(
            original.decode("utf-8", "replace").splitlines(keepends=True),
            mutant.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
            fromfile=source.name,
            tofile=mutant.name,
        )
        (survivors / f"{mutant.stem}.diff").write_text("".join(diff), encoding="utf-8")

    # -- report ---------------------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        """Return ``{status: count}`` over every outcome so far."""
        counts = {"KILLED": 0, "SURVIVED": 0, "STILLBORN": 0, "EQUIVALENT": 0}
        for outcome in self.outcomes:
            counts[outcome.status] += 1
        return counts

    def kill_rate(self) -> float | None:
        """Return killed / (killed + survived) in percent, None when nothing was run."""
        counts = self.counts()
        tested = counts["KILLED"] + counts["SURVIVED"]
        return None if tested == 0 else 100.0 * counts["KILLED"] / tested

    def report(self) -> str:
        """Write ``report.txt`` + ``results.json`` under ``out`` and return the report text."""
        counts = self.counts()
        rate = self.kill_rate()
        lines = [
            "MUTATION RUN (report-only)",
            f"killed {counts['KILLED']}  survived {counts['SURVIVED']}  "
            f"stillborn {counts['STILLBORN']}  equivalent {counts['EQUIVALENT']}",
            "kill rate: " + ("n/a" if rate is None else f"{rate:.1f}%"),
            "",
        ]
        lines.extend(
            f"SURVIVED  {o.source}  {o.mutant}  -> survivors/{Path(o.mutant).stem}.diff"
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
    parser.add_argument("--out", default="build/mutation", help="output folder")
    parser.add_argument("--sample", type=int, default=0, help="mutants per source, 0 = all")
    parser.add_argument("--seed", type=int, default=1, help="sampling seed")
    args = parser.parse_args(argv)
    run = MutationRun(args.root, args.out, sample=args.sample, seed=args.seed)
    try:
        for src in args.sources:
            results = run.run_source(Path(args.root) / src)
            killed = sum(r.status == "KILLED" for r in results)
            sys.stdout.write(f"{src}: {killed} killed, {len(results) - killed} survived\n")
    except MutationError as ex:
        sys.stdout.write(f"MUTATION RUN FAILED: {ex}\n")
        return 1
    sys.stdout.write(run.report())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
