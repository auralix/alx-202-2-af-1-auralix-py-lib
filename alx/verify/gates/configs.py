# SPDX-License-Identifier: MIT
"""Configuration matrix gate: every row compiled clean, and every line compiled by some row.

Reads the ``report.json`` that :func:`alx.verify.configs.run_matrix` writes and fails when

* the run was not the whole matrix (a subset proves nothing about the lines no row reached),
* any row has a finding, a missing header or an undeclared identifier,
* any measured line of code was compiled by no row and no exemption explains it.

Usage::

    python -m alx.verify.gates.configs build/matrix/report.json [--out report.txt]

Exit code 0 = PASS, 1 = FAIL. Every finding is one line naming the row or the file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def check(report: dict[str, Any]) -> list[str]:
    """Return every finding of one matrix report; an empty list is a PASS."""
    findings: list[str] = []
    arguments = report["source"]["arguments"]
    rows = report["rows"]
    if arguments["only"] or len(rows) != arguments["of"]:
        findings.append(
            f"partial run: {len(rows)} of {arguments['of']} rows (only {arguments['only']}); "
            "the gate reads the whole matrix"
        )
    if not rows:
        findings.append("no row was run")
    for row in rows:
        if row["status"] == "PASS":
            continue
        what = [f"{row['findings_total']} finding(s)"]
        if row["missing_headers"]:
            what.append(f"missing {', '.join(row['missing_headers'])}")
        if row["undeclared"]:
            what.append(f"undeclared {', '.join(row['undeclared'])}")
        findings.append(f"{row['name']}: {row['status']}, {'; '.join(what)}")
    for file, runs in report["uncovered"].items():
        for first, last, guard, *_ in runs:
            span = str(first) if first == last else f"{first}-{last}"
            findings.append(f"{file}:{span}: compiled by no row, hidden by {guard}")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.gates.configs", description=__doc__)
    parser.add_argument("report", type=Path, help="report.json of a matrix run")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    findings = check(report)
    totals = report["coverage"]
    scope = (
        f"{len(report['rows'])} rows, {totals['code_lines']} code lines, "
        f"{totals['covered']} covered, {totals['exempt']} exempt"
    )
    verdict = f"FAIL ({len(findings)} finding(s); {scope})" if findings else f"PASS ({scope})"
    text = "\n".join([f"CONFIGS GATE: {verdict}", *findings]) + "\n"
    sys.stdout.write(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
