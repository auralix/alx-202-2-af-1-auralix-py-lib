# SPDX-License-Identifier: MIT
"""alx.verify.gates.configs: the verdict over a configuration matrix report.

Proofs (ALX-1564):
  P367 a full run whose rows all pass and whose every line some row compiled is a PASS; a partial
       run, a run of no rows, a row with findings, missing headers or undeclared names, and a line
       no row compiled are each a finding naming the row or the file; main() exits 0 on PASS, 1 on
       FAIL, and --out writes the same text
"""

import copy
import json
from typing import Any

from alx.verify.gates import configs as gate

CLEAN: dict[str, Any] = {
    "source": {"arguments": {"only": [], "rows": 1, "of": 1}},
    "rows": [
        {
            "name": "0001_gcc_p_all_app_lib",
            "status": "PASS",
            "findings_total": 0,
            "missing_headers": [],
            "undeclared": [],
        }
    ],
    "coverage": {"files": 2, "code_lines": 10, "covered": 9, "exempt": 1, "uncovered": 0},
    "uncovered": {},
}


def test_ALX1564_P367_the_gate_passes_only_a_whole_clean_and_covered_matrix(tmp_path, capsys):
    assert gate.check(CLEAN) == []

    partial = copy.deepcopy(CLEAN)
    partial["source"]["arguments"] = {"only": ["0001"], "rows": 1, "of": 1}
    assert gate.check(partial)[0].startswith("partial run: 1 of 1 rows (only ['0001'])")
    fewer = copy.deepcopy(CLEAN)
    fewer["source"]["arguments"]["of"] = 2
    assert gate.check(fewer)[0].startswith("partial run: 1 of 2 rows")

    empty = copy.deepcopy(CLEAN)
    empty["rows"] = []
    empty["source"]["arguments"]["of"] = 0
    assert gate.check(empty) == ["no row was run"]

    broken = copy.deepcopy(CLEAN)
    broken["rows"].append(
        {
            "name": "0002_gcc_p_all_app_lib",
            "status": "NO SHIM",
            "findings_total": 3,
            "missing_headers": ["vendor.h"],
            "undeclared": ["HAL_Init", "GPIOA"],
        }
    )
    broken["rows"].append(
        {
            "name": "0003_gcc_p_all_app_lib",
            "status": "FAIL",
            "findings_total": 1,
            "missing_headers": [],
            "undeclared": [],
        }
    )
    broken["source"]["arguments"]["of"] = 3
    broken["uncovered"] = {
        "lib/a.c": [[4, 4, "#if defined(X)"], [7, 9, "#else (after #if X)", "x"]]
    }
    assert gate.check(broken) == [
        "0002_gcc_p_all_app_lib: NO SHIM, 3 finding(s); missing vendor.h; undeclared HAL_Init, GPIOA",
        "0003_gcc_p_all_app_lib: FAIL, 1 finding(s)",
        "lib/a.c:4: compiled by no row, hidden by #if defined(X)",
        "lib/a.c:7-9: compiled by no row, hidden by #else (after #if X)",
    ]

    good = tmp_path / "good.json"
    good.write_text(json.dumps(CLEAN), encoding="utf-8")
    assert gate.main([str(good)]) == 0
    assert capsys.readouterr().out == (
        "CONFIGS GATE: PASS (1 rows, 10 code lines, 9 covered, 1 exempt)\n"
    )
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(broken), encoding="utf-8")
    out = tmp_path / "evidence" / "gate.txt"
    assert gate.main([str(bad), "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert printed.startswith("CONFIGS GATE: FAIL (4 finding(s); 3 rows, 10 code lines")
    assert out.read_text(encoding="utf-8") == printed
