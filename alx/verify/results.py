# SPDX-License-Identifier: MIT
"""Read local pytest JUnit evidence without counting expected failures as passes.

Execution location belongs to the invoking runner. A report's filename or the location
of its Python controller cannot establish where the code under test executed.

:func:`write_functionality_report` turns a target run into the reviewable matrix a repository
keeps as evidence: its own map of test module to functionality in, one record per case out.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

Outcome = Literal["passed", "failed", "error", "skipped", "xfail"]


@dataclass(frozen=True)
class CaseResult:
    """One JUnit testcase record, preserving properties and distinct diagnostics."""

    name: str
    module: str
    outcome: Outcome
    reason: str
    seconds: float
    properties: tuple[tuple[str, str], ...]


def read_cases(path: Path) -> list[CaseResult]:
    """Read a local pytest report; reject empty or unrelated XML as missing evidence."""
    root = ET.parse(path).getroot()  # noqa: S314 - input is local pytest output
    if root.tag not in {"testsuite", "testsuites"}:
        msg = f"not a JUnit report: {path}"
        raise ValueError(msg)
    results = []
    for case in root.iter("testcase"):
        outcome: Outcome = "passed"
        reasons = []
        states: tuple[tuple[str, Outcome], ...] = (
            ("error", "error"),
            ("failure", "failed"),
            ("skipped", "skipped"),
        )
        for tag, status in states:
            for detail in case.findall(tag):
                if outcome == "passed":
                    outcome = (
                        "xfail"
                        if tag == "skipped" and detail.get("type") == "pytest.xfail"
                        else status
                    )
                reason = detail.get("message", "") or (detail.text or "").strip()
                if reason and reason not in reasons:
                    reasons.append(reason)
        results.append(
            CaseResult(
                name=case.attrib["name"],
                module=case.get("classname", ""),
                outcome=outcome,
                reason="\n".join(reasons),
                seconds=float(case.get("time", "0")),
                properties=tuple(
                    (p.get("name", ""), p.get("value", ""))
                    for p in case.findall("properties/property")
                ),
            )
        )
    if not results:
        msg = f"no test cases in report: {path}"
        raise ValueError(msg)
    return results


OUTCOMES: tuple[Outcome, ...] = ("passed", "xfail", "skipped", "failed", "error")
"""The matrix columns, in order: an expected failure and a skip are counted apart from a pass."""

TARGET_PACKAGE = "tests.target."
"""pytest names a case after its dotted module; a target suite is the package ``tests.target``."""


def _module(case: CaseResult, package: str) -> str:
    """Return the module of a case, with or without the package prefix."""
    return case.module.removeprefix(package)


def write_functionality_report(
    run_dir: Path,
    modes: Sequence[str],
    functionalities: Mapping[str, str],
    *,
    notes: Sequence[str] = (),
    package: str = TARGET_PACKAGE,
) -> bool:
    """Write ``functionality.md`` and ``.json`` for one target run; return whether nothing failed.

    Each mode is a folder of ``run_dir`` holding its own ``pytest_report.xml`` - the same suite run
    in another configuration. Every case is classified by its module into the functionality that
    ``functionalities`` names for it; a module the map does not name is refused, because it means
    the input mixes in results from somewhere else, host checks most likely. Every case, metric and
    non-pass reason is kept, one record per case; ``notes`` are the repository's own caveats about
    what the run does not prove.
    """
    records: list[dict[str, object]] = []
    lines = [
        "# HIL functionality evidence",
        "",
        f"Run: `{run_dir.name}`.",
        "",
        "Counts are target JUnit testcase records, separated by configuration.",
        "Pytest can emit separate records for a body failure and its teardown error.",
        "Expected failures and skips are coverage gaps, not passing functionality.",
        *notes,
        "",
        "| Mode | Functionality | Passed | Xfail | Skipped | Failed | Error |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for mode in modes:
        report = run_dir / mode / "pytest_report.xml"
        cases = read_cases(report)
        unknown = {_module(case, package) for case in cases} - functionalities.keys()
        if unknown:
            known = "possibly mixed host input"
            msg = f"unclassified target report modules ({known}): {sorted(unknown)}"
            raise ValueError(msg)
        for module, functionality in functionalities.items():
            counts = Counter(case.outcome for case in cases if _module(case, package) == module)
            if counts:
                values = " | ".join(str(counts[key]) for key in OUTCOMES)
                lines.append(f"| {mode} | {functionality} | {values} |")
        records.extend(
            {
                "mode": mode,
                "execution_location": "target",
                "report": str(report.relative_to(run_dir)),
                "functionality": functionalities[_module(case, package)],
                **asdict(case),
            }
            for case in cases
        )
    lines += ["", "## Case evidence", "", "| Mode | Proof / case | Outcome | Diagnostic |"]
    lines.append("|---|---|---|---|")
    for record in records:
        reason = str(record["reason"]).replace("|", "/").replace("\n", " ")
        lines.append(f"| {record['mode']} | `{record['name']}` | {record['outcome']} | {reason} |")
    text = json.dumps(records, indent=2) + "\n"
    (run_dir / "functionality.json").write_text(text, encoding="utf-8")
    (run_dir / "functionality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return all(record["outcome"] not in {"failed", "error"} for record in records)
