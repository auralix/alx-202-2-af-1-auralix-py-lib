# SPDX-License-Identifier: MIT
"""alx.verify.coverage_gate - the per-file lines-and-branches gate over a coverage.py JSON report.

Proofs (ALX-1544):
  P103 lines and branches are gated separately: full lines with a missed branch fails
  P104 an explicit file list gates only those files, with path separators normalized; a listed file
       missing from the report fails
  P105 main() prints and writes the report; PASS exits 0, FAIL exits 1
"""

import json

from alx.verify import coverage_gate


def summary(statements, covered_lines, branches, covered_branches, partial=0, missing=()):
    return {
        "summary": {
            "num_statements": statements,
            "covered_lines": covered_lines,
            "num_branches": branches,
            "covered_branches": covered_branches,
            "num_partial_branches": partial,
        },
        "missing_lines": list(missing),
    }


REPORT = {
    "files": {
        "alx\\c_lib\\cli.py": summary(146, 146, 42, 41, partial=1),
        "alx/errors.py": summary(3, 3, 0, 0),
        "alx/serial_logger.py": summary(191, 171, 48, 40, partial=6, missing=(138, 181, 182)),
    }
}


def test_ALX1544_P103_lines_and_branches_are_gated_separately():
    rows, failures = coverage_gate.evaluate(REPORT, 100.0)
    assert len(rows) == 3
    assert failures == [
        "alx/c_lib/cli.py: branches 97.6% < 100%",
        "alx/serial_logger.py: lines 89.5% < 100%",
        "alx/serial_logger.py: branches 83.3% < 100%",
    ]
    assert "missing 138,181,182" in rows[2]
    assert "partial branches 1" in rows[0]
    assert coverage_gate.evaluate(REPORT, 80.0)[1] == []


def test_ALX1544_P104_file_list_filters_and_normalizes_paths_missing_file_fails():
    rows, failures = coverage_gate.evaluate(REPORT, 100.0, ["alx/c_lib/cli.py", "alx/errors.py"])
    assert [r.split()[0] for r in rows] == ["alx/c_lib/cli.py", "alx/errors.py"]
    assert failures == ["alx/c_lib/cli.py: branches 97.6% < 100%"]
    _, failures = coverage_gate.evaluate(REPORT, 100.0, ["alx\\errors.py", "alx/fw/live_watch.py"])
    assert failures == ["alx/fw/live_watch.py: not in the report"]


def test_ALX1544_P105_main_writes_the_report_and_exits_per_verdict(tmp_path, capsys):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    out = tmp_path / "gate.txt"
    assert coverage_gate.main([str(report), "--min", "100", "--out", str(out)]) == 1
    text = out.read_text(encoding="ascii")
    assert text.startswith("COVERAGE GATE: FAIL (min 100% lines and branches, 3 files)")
    assert "  alx/serial_logger.py: lines 89.5% < 100%" in text
    assert coverage_gate.main([str(report), "--min", "80", "alx/errors.py"]) == 0
    assert "COVERAGE GATE: PASS (min 80% lines and branches, 1 files)" in capsys.readouterr().out
