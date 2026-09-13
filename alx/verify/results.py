# SPDX-License-Identifier: MIT
"""Read local pytest JUnit evidence without counting expected failures as passes.

Execution location belongs to the invoking runner. A report's filename or the location
of its Python controller cannot establish where the code under test executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from pathlib import Path

Outcome = Literal["passed", "failed", "error", "skipped", "xfail"]


@dataclass(frozen=True)
class CaseResult:
    """One reported case, preserving its proof properties and diagnostic."""

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
        reason = ""
        states: tuple[tuple[str, Outcome], ...] = (
            ("error", "error"),
            ("failure", "failed"),
            ("skipped", "skipped"),
        )
        for tag, status in states:
            detail = case.find(tag)
            if detail is not None:
                outcome = "xfail" if detail.get("type") == "pytest.xfail" else status
                reason = detail.get("message", "") or (detail.text or "").strip()
                break
        results.append(
            CaseResult(
                name=case.attrib["name"],
                module=case.get("classname", ""),
                outcome=outcome,
                reason=reason,
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
