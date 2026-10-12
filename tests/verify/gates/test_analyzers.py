# SPDX-License-Identifier: MIT
"""alx.verify.gates.analyzers: the analysis report as a PASS/FAIL gate.

Proofs (ALX-1564):
  P694 every named row clean in both variants and every line compiled is a PASS; a partial run, a
       variant not run, no row, a finding and a line no analysed row compiled are each named
  P695 main() exits 0 on PASS and 1 on FAIL, counts the findings without their listed lines, and
       --out writes the report
"""

import copy
import json
from typing import Any

from alx.verify.gates import analyzers as gate

CLEAN: dict[str, Any] = {
    "source": {"arguments": {"only": [], "rows": ["0001_a", "0002_b"], "of": 2}},
    "runs": [
        {"row": row, "variant": variant, "findings_total": 0, "findings": []}
        for row in ("0001_a", "0002_b")
        for variant in ("configured", "disabled")
    ],
    "coverage": {"files": 3, "code_lines": 40, "covered": 38, "exempt": 2, "uncovered": 0},
    "uncovered": {},
}


def test_ALX1564_P694_a_clean_report_passes_and_every_failure_is_named():
    assert gate.check(CLEAN) == []
    bad = copy.deepcopy(CLEAN)
    bad["source"]["arguments"] = {"only": ["0001"], "rows": ["0001_a"], "of": 2}
    bad["runs"] = bad["runs"][:1]
    bad["runs"][0].update(
        findings_total=2,
        findings=["a.c:3: clang-tidy warning: x [c]", "a.c:4: cppcheck style: y [s]"],
    )
    bad["uncovered"] = {"lib/a.c": [[7, 7, "#else (after #if defined(FEAT))"], [9, 12, "#if X"]]}
    assert gate.check(bad) == [
        "partial run: 1 of 2 rows (only ['0001']); the gate reads every named row",
        "0001_a: not analysed disabled",
        "0001_a configured: 2 finding(s)",
        "  a.c:3: clang-tidy warning: x [c]",
        "  a.c:4: cppcheck style: y [s]",
        "lib/a.c:7: compiled by no analysed row, hidden by #else (after #if defined(FEAT))",
        "lib/a.c:9-12: compiled by no analysed row, hidden by #if X",
    ]
    empty = copy.deepcopy(CLEAN)
    empty["source"]["arguments"] = {"only": [], "rows": [], "of": 0}
    empty["runs"] = []
    assert gate.check(empty) == ["no row was analysed"]


def test_ALX1564_P695_main_reports_pass_and_fail(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(CLEAN), encoding="utf-8")
    assert gate.main([str(good)]) == 0
    assert capsys.readouterr().out == (
        "ANALYZERS GATE: PASS (2 rows x 2, 40 code lines, 38 covered, 2 exempt)\n"
    )
    bad = copy.deepcopy(CLEAN)
    bad["runs"][1].update(findings_total=1, findings=["a.c:3: cppcheck style: y [s]"])
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    out = tmp_path / "gate" / "analyzers_gate.txt"
    assert gate.main([str(path), "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert printed == (
        "ANALYZERS GATE: FAIL (1 finding(s); 2 rows x 2, 40 code lines, 38 covered, 2 exempt)\n"
        "0001_a disabled: 1 finding(s)\n"
        "  a.c:3: cppcheck style: y [s]\n"
    )
    assert out.read_text(encoding="utf-8") == printed
