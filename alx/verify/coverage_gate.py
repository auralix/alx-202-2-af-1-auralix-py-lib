# SPDX-License-Identifier: MIT
"""Coverage gate over a coverage.py JSON report: each file meets the minimum in lines AND branches.

The C library gates llvm-cov's summary (lines, branches, regions, functions); coverage.py measures
lines and branches, so those two are gated here, each on its own: the combined percentage that
coverage.py prints would let missing branches hide behind covered lines. Usage::

    python -m alx.verify.coverage_gate build/cov/coverage.json --min 100 [--out gate.txt] [file ...]

Without file arguments every file in the report is gated. Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * covered / total


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def evaluate(
    report: dict[str, Any], minimum: float, files: Iterable[str] = ()
) -> tuple[list[str], list[str]]:
    """Return ``(rows, failures)``: a row per gated file, a failure per metric below ``minimum``."""
    wanted = {_norm(f) for f in files}
    rows: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()
    for name, data in sorted(report["files"].items(), key=lambda kv: _norm(kv[0])):
        norm = _norm(name)
        if wanted and norm not in wanted:
            continue
        seen.add(norm)
        summary = data["summary"]
        lines = _percent(summary["covered_lines"], summary["num_statements"])
        branches = _percent(summary["covered_branches"], summary["num_branches"])
        missing = ",".join(str(n) for n in data.get("missing_lines", []))
        partial = summary["num_partial_branches"]
        rows.append(
            f"{norm:<40} lines {lines:6.1f}%  branches {branches:6.1f}%"
            + (f"  missing {missing}" if missing else "")
            + (f"  partial branches {partial}" if partial else "")
        )
        if lines < minimum:
            failures.append(f"{norm}: lines {lines:.1f}% < {minimum:g}%")
        if branches < minimum:
            failures.append(f"{norm}: branches {branches:.1f}% < {minimum:g}%")
    failures.extend(f"{name}: not in the report" for name in sorted(wanted - seen))
    return rows, failures


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.coverage_gate", description=__doc__)
    parser.add_argument(
        "report", help="coverage.py JSON report (coverage json / --cov-report=json)"
    )
    parser.add_argument(
        "--min", type=float, default=100.0, help="minimum percent, lines and branches"
    )
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument(
        "files", nargs="*", help="files to gate (default: every file in the report)"
    )
    args = parser.parse_intermixed_args(argv)  # files may follow --min / --out
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows, failures = evaluate(report, args.min, args.files)
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
