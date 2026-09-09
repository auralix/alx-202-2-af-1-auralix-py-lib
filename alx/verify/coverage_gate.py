# SPDX-License-Identifier: MIT
"""Coverage gate: each file of a coverage report meets the minimum in every gated metric.

Three report formats, one gate for every language of the pipeline:

* cobertura XML - what coverage.py (``--cov-report=xml``), lcov-cobertura (the C library's llvm-cov
  lane) and coverlet (C#) all write: lines and branches
* llvm-cov export JSON (``llvm-cov export -summary-only``): lines, branches, regions and functions,
  the C library's native report
* coverage.py JSON (``--cov-report=json``): lines and branches with the missing-line detail

Every metric is gated on its own: a combined percentage would let missing branches hide behind
covered lines. A metric with nothing to cover (no branches in the file) counts as 100 %. Usage::

    python -m alx.verify.coverage_gate build/coverage/coverage.xml --min 100 [--out f] [file ...]
    python -m alx.verify.coverage_gate summary.json --metrics lines,branches,regions,functions foo.c

Without file arguments every file in the report is gated; a file argument matches the report's path
exactly or by its trailing components (``alxFoo.c`` matches ``C:/repo/alxFoo.c``). Exit code 0 =
PASS, 1 = FAIL.
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

METRICS = ("lines", "branches", "regions", "functions")
DEFAULT_METRICS = ("lines", "branches")


@dataclass(frozen=True)
class FileCoverage:
    """Per-file figures a report yields, normalized across formats (None = the format lacks it)."""

    name: str
    lines_pct: float
    branches_pct: float
    missing_lines: tuple[int, ...] = ()
    partial_branches: int = 0
    regions_pct: float | None = None
    functions_pct: float | None = None

    def metric(self, name: str) -> float | None:
        """Return the percentage of ``name`` (one of METRICS), None when the report lacks it."""
        value: float | None = getattr(self, f"{name}_pct")
        return value


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


def from_llvm(report: dict[str, Any]) -> list[FileCoverage]:
    """Read an llvm-cov export (``-summary-only``): lines, branches, regions and functions."""
    files = []
    for entry in report["data"][0]["files"]:
        summary = entry["summary"]

        def pct(metric: str, summary: dict[str, Any] = summary) -> float:
            block = summary.get(metric, {})
            return _percent(int(block.get("covered", 0)), int(block.get("count", 0)))

        files.append(
            FileCoverage(
                _norm(entry["filename"]),
                pct("lines"),
                pct("branches"),
                regions_pct=pct("regions"),
                functions_pct=pct("functions"),
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
    """Load a report by content: ``.xml`` cobertura; JSON is llvm-cov export or coverage.py."""
    path = Path(path)
    if path.suffix.lower() == ".xml":
        # the report is written by our own coverage tool, not untrusted input
        return from_cobertura(ET.parse(path).getroot())  # noqa: S314
    report = json.loads(path.read_text(encoding="utf-8"))
    if str(report.get("type", "")).startswith("llvm.coverage"):
        return from_llvm(report)
    return from_coverage_json(report)


def _matches(name: str, wanted: str) -> bool:
    return name == wanted or name.endswith("/" + wanted)


def evaluate(
    files: Iterable[FileCoverage],
    minimum: float,
    wanted: Iterable[str] = (),
    metrics: Iterable[str] = DEFAULT_METRICS,
) -> tuple[list[str], list[str]]:
    """Return ``(rows, failures)``: a row per gated file, a failure per metric under the minimum.

    ``wanted`` narrows the gate to those files (exact path or trailing components); a wanted file
    absent from the report is a failure. ``metrics`` names what is gated; a gated metric the report
    does not carry is a failure too.
    """
    want = [_norm(f) for f in wanted]
    gated = tuple(metrics)
    unknown = [m for m in gated if m not in METRICS]
    if unknown:
        raise ValueError(f"unknown metric(s) {unknown}; known: {', '.join(METRICS)}")
    rows: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()
    for fc in sorted(files, key=lambda f: f.name):
        hits = [w for w in want if _matches(fc.name, w)]
        if want and not hits:
            continue
        seen.update(hits)
        cells = [f"{m} {value:6.1f}%" for m in METRICS if (value := fc.metric(m)) is not None]
        missing = ",".join(str(n) for n in fc.missing_lines)
        rows.append(
            f"{fc.name:<40} "
            + "  ".join(cells)
            + (f"  missing {missing}" if missing else "")
            + (f"  partial branches {fc.partial_branches}" if fc.partial_branches else "")
        )
        for m in gated:
            value = fc.metric(m)
            if value is None:
                failures.append(f"{fc.name}: {m} not in the report")
            elif value < minimum:
                failures.append(f"{fc.name}: {m} {value:.1f}% < {minimum:g}%")
    failures.extend(f"{name}: not in the report" for name in want if name not in seen)
    return rows, failures


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.coverage_gate", description=__doc__)
    parser.add_argument("report", help="cobertura .xml, llvm-cov export .json or coverage.py .json")
    parser.add_argument(
        "--min", type=float, default=100.0, help="minimum percent for every gated metric"
    )
    parser.add_argument(
        "--metrics",
        default=",".join(DEFAULT_METRICS),
        help=f"comma list of gated metrics, from {', '.join(METRICS)} (default lines,branches)",
    )
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument(
        "files", nargs="*", help="files to gate (default: every file in the report)"
    )
    args = parser.parse_intermixed_args(argv)  # files may follow the options
    metrics = tuple(m.strip() for m in args.metrics.split(",") if m.strip())
    rows, failures = evaluate(load(args.report), args.min, args.files, metrics)
    verdict = "FAIL" if failures else "PASS"
    lines = [
        f"COVERAGE GATE: {verdict} (min {args.min:g}% on {', '.join(metrics)}, {len(rows)} files)"
    ]
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
