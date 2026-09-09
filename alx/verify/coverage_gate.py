# SPDX-License-Identifier: MIT
"""Coverage gate: each file of a coverage report meets the minimum in lines AND branches.

Two report formats, one gate for every language of the pipeline:

* cobertura XML - what coverage.py (``--cov-report=xml``), lcov-cobertura (the C library's llvm-cov
  lane) and coverlet (C#) all write: the common currency
* coverage.py JSON (``--cov-report=json``) - richer per-file detail when the report is Python's own

Lines and branches are gated each on its own: a combined percentage would let missing branches hide
behind covered lines. Usage::

    python -m alx.verify.coverage_gate build/cov/coverage.xml --min 100 [--out gate.txt] [file ...]

Without file arguments every file in the report is gated. Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class FileCoverage:
    """Per-file figures a report yields, normalized across formats."""

    name: str
    lines_pct: float
    branches_pct: float
    missing_lines: tuple[int, ...] = ()
    partial_branches: int = 0


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * covered / total


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def from_coverage_json(report: dict[str, Any]) -> list[FileCoverage]:
    """Read a coverage.py JSON report (``coverage json`` / ``--cov-report=json``)."""
    files = []
    for name, data in report["files"].items():
        summary = data["summary"]
        files.append(
            FileCoverage(
                _norm(name),
                _percent(summary["covered_lines"], summary["num_statements"]),
                _percent(summary["covered_branches"], summary["num_branches"]),
                tuple(int(n) for n in data.get("missing_lines", [])),
                int(summary.get("num_partial_branches", 0)),
            )
        )
    return files


def from_cobertura(root: ET.Element) -> list[FileCoverage]:
    """Read a cobertura XML tree: one entry per ``<class filename=...>`` with its rates."""
    files = []
    for cls in root.iter("class"):
        name = _norm(cls.get("filename") or cls.get("name") or "?")
        lines = list(cls.iter("line"))
        missing = tuple(int(line.get("number", "0")) for line in lines if line.get("hits") == "0")
        partial = sum(
            1
            for line in lines
            if line.get("branch") == "true"
            and not line.get("condition-coverage", "").startswith("100%")
        )
        files.append(
            FileCoverage(
                name,
                100.0 * float(cls.get("line-rate", "0")),
                100.0 * float(cls.get("branch-rate", "1")),
                missing,
                partial,
            )
        )
    return files


def load(path: str | Path) -> list[FileCoverage]:
    """Load a report by suffix: ``.xml`` cobertura, anything else coverage.py JSON."""
    path = Path(path)
    if path.suffix.lower() == ".xml":
        # the report is written by our own coverage tool, not untrusted input
        return from_cobertura(ET.parse(path).getroot())  # noqa: S314
    return from_coverage_json(json.loads(path.read_text(encoding="utf-8")))


def evaluate(
    files: Iterable[FileCoverage], minimum: float, wanted: Iterable[str] = ()
) -> tuple[list[str], list[str]]:
    """Return ``(rows, failures)``: a row per gated file, a failure per metric below ``minimum``."""
    want = {_norm(f) for f in wanted}
    rows: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()
    for fc in sorted(files, key=lambda f: f.name):
        if want and fc.name not in want:
            continue
        seen.add(fc.name)
        missing = ",".join(str(n) for n in fc.missing_lines)
        rows.append(
            f"{fc.name:<40} lines {fc.lines_pct:6.1f}%  branches {fc.branches_pct:6.1f}%"
            + (f"  missing {missing}" if missing else "")
            + (f"  partial branches {fc.partial_branches}" if fc.partial_branches else "")
        )
        if fc.lines_pct < minimum:
            failures.append(f"{fc.name}: lines {fc.lines_pct:.1f}% < {minimum:g}%")
        if fc.branches_pct < minimum:
            failures.append(f"{fc.name}: branches {fc.branches_pct:.1f}% < {minimum:g}%")
    failures.extend(f"{name}: not in the report" for name in sorted(want - seen))
    return rows, failures


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.coverage_gate", description=__doc__)
    parser.add_argument("report", help="cobertura .xml or coverage.py .json report")
    parser.add_argument(
        "--min", type=float, default=100.0, help="minimum percent, lines and branches"
    )
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument(
        "files", nargs="*", help="files to gate (default: every file in the report)"
    )
    args = parser.parse_intermixed_args(argv)  # files may follow --min / --out
    rows, failures = evaluate(load(args.report), args.min, args.files)
    verdict = "FAIL" if failures else "PASS"
    lines = [f"COVERAGE GATE: {verdict} (min {args.min:g}% lines and branches, {len(rows)} files)"]
    lines += rows
    lines += [f"  {f}" for f in failures]
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="ascii")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
