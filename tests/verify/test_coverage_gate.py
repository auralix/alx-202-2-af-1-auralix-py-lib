# SPDX-License-Identifier: MIT
"""alx.verify.coverage_gate - the per-file lines-and-branches gate over cobertura XML or coverage.py JSON.

Proofs (ALX-1544):
  P103 lines and branches are gated separately: full lines with a missed branch fails
  P104 an explicit file list gates only those files, with path separators normalized; a listed file
       missing from the report fails
  P105 main() prints and writes the report; PASS exits 0, FAIL exits 1
  P113 a cobertura report (the C library's and the C# lane's format, coverage.py's XML) yields the same
       figures as the JSON report: rates per class, missing lines, partial branches
"""

import json

import pytest

from alx.verify import coverage_gate
from alx.verify.coverage_gate import FileCoverage


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

COBERTURA = """<?xml version="1.0" ?>
<coverage version="7.6" line-rate="0.98" branch-rate="0.95">
  <packages>
    <package name="alx.c_lib" line-rate="1" branch-rate="0.976">
      <classes>
        <class name="cli.py" filename="alx\\c_lib\\cli.py" line-rate="1" branch-rate="0.97619">
          <lines>
            <line number="74" hits="5" branch="true" condition-coverage="50% (1/2)"/>
            <line number="75" hits="5"/>
          </lines>
        </class>
      </classes>
    </package>
    <package name="alx" line-rate="0.9" branch-rate="0.85">
      <classes>
        <class name="errors.py" filename="alx/errors.py" line-rate="1" branch-rate="1">
          <lines><line number="8" hits="1"/></lines>
        </class>
        <class name="serial_logger.py" filename="alx/serial_logger.py" line-rate="0.8953" branch-rate="0.8333">
          <lines>
            <line number="137" hits="2" branch="true" condition-coverage="100% (2/2)"/>
            <line number="138" hits="0"/>
            <line number="181" hits="0"/>
            <line number="182" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def test_ALX1544_P103_lines_and_branches_are_gated_separately():
    files = coverage_gate.from_coverage_json(REPORT)
    rows, failures = coverage_gate.evaluate(files, 100.0)
    assert len(rows) == 3
    assert failures == [
        "alx/c_lib/cli.py: branches 97.6% < 100%",
        "alx/serial_logger.py: lines 89.5% < 100%",
        "alx/serial_logger.py: branches 83.3% < 100%",
    ]
    assert "missing 138,181,182" in rows[2]
    assert "partial branches 1" in rows[0]
    assert coverage_gate.evaluate(files, 80.0)[1] == []


def test_ALX1544_P104_file_list_filters_and_normalizes_paths_missing_file_fails():
    files = coverage_gate.from_coverage_json(REPORT)
    rows, failures = coverage_gate.evaluate(files, 100.0, ["alx/c_lib/cli.py", "alx/errors.py"])
    assert [r.split()[0] for r in rows] == ["alx/c_lib/cli.py", "alx/errors.py"]
    assert failures == ["alx/c_lib/cli.py: branches 97.6% < 100%"]
    _, failures = coverage_gate.evaluate(files, 100.0, ["alx\\errors.py", "alx/fw/live_watch.py"])
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


def test_ALX1544_P113_cobertura_report_yields_the_same_figures(tmp_path):
    xml = tmp_path / "coverage.xml"
    xml.write_text(COBERTURA, encoding="utf-8")
    cli, err, sl = sorted(coverage_gate.load(xml), key=lambda f: f.name)
    assert (cli.name, cli.lines_pct, cli.missing_lines, cli.partial_branches) == (
        "alx/c_lib/cli.py",
        100.0,
        (),
        1,
    )
    assert cli.branches_pct == pytest.approx(97.619)
    assert err == FileCoverage("alx/errors.py", 100.0, 100.0, (), 0)
    assert (sl.name, sl.missing_lines, sl.partial_branches) == (
        "alx/serial_logger.py",
        (138, 181, 182),
        0,
    )
    assert sl.lines_pct == pytest.approx(89.53)
    assert sl.branches_pct == pytest.approx(83.33)
    _, failures = coverage_gate.evaluate([cli, err, sl], 100.0)
    assert failures == [
        "alx/c_lib/cli.py: branches 97.6% < 100%",
        "alx/serial_logger.py: lines 89.5% < 100%",
        "alx/serial_logger.py: branches 83.3% < 100%",
    ]
    assert coverage_gate.main([str(xml), "--min", "80"]) == 0
