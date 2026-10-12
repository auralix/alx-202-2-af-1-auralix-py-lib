# SPDX-License-Identifier: MIT
"""Static analysis gate: every analysed row clean in both variants, every line compiled by them.

Reads the ``report.json`` that :func:`alx.verify.analyzers.run_analysis` writes and fails when

* the run was not every named row, each as configured and with its checks disabled,
* any row has a finding of clang-tidy or cppcheck in either variant,
* any measured line of code was compiled by no analysed row and no exemption explains it.

Usage::

    python -m alx.verify.gates.analyzers build/analyze/matrix/report.json [--out report.txt]

Exit code 0 = PASS, 1 = FAIL. Every finding is one line naming the row or the file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

VARIANTS = ("configured", "disabled")


def check(report: dict[str, Any]) -> list[str]:
    """Return every finding of one analysis report; an empty list is a PASS."""
    findings: list[str] = []
    arguments = report["source"]["arguments"]
    rows = arguments["rows"]
    if arguments["only"] or len(rows) != arguments["of"]:
        findings.append(
            f"partial run: {len(rows)} of {arguments['of']} rows (only {arguments['only']}); "
            "the gate reads every named row"
        )
    ran = {(run["row"], run["variant"]) for run in report["runs"]}
    findings += [
        f"{row}: not analysed {variant}"
        for row in rows
        for variant in VARIANTS
        if (row, variant) not in ran
    ]
    if not rows:
        findings.append("no row was analysed")
    for run in report["runs"]:
        if run["findings_total"]:
            findings.append(f"{run['row']} {run['variant']}: {run['findings_total']} finding(s)")
            findings += [f"  {line}" for line in run["findings"]]
    for file, runs in report["uncovered"].items():
        for first, last, guard, *_ in runs:
            span = str(first) if first == last else f"{first}-{last}"
            findings.append(f"{file}:{span}: compiled by no analysed row, hidden by {guard}")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(
        prog="python -m alx.verify.gates.analyzers", description=__doc__
    )
    parser.add_argument("report", type=Path, help="report.json of an analysis run")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    findings = check(report)
    totals = report["coverage"]
    scope = (
        f"{len(report['source']['arguments']['rows'])} rows x {len(VARIANTS)}, "
        f"{totals['code_lines']} code lines, {totals['covered']} covered, {totals['exempt']} exempt"
    )
    failed = sum(1 for f in findings if not f.startswith("  "))
    verdict = f"FAIL ({failed} finding(s); {scope})" if findings else f"PASS ({scope})"
    text = "\n".join([f"ANALYZERS GATE: {verdict}", *findings]) + "\n"
    sys.stdout.write(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
