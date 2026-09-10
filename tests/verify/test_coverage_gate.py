# SPDX-License-Identifier: MIT
"""alx.verify.coverage_gate - the per-file lines-and-branches gate over cobertura XML or coverage.py JSON.

Proofs (ALX-1544):
  P103 lines and branches are gated separately: full lines with a missed branch fails
  P104 an explicit file list gates only those files, with path separators normalized; a listed file
       missing from the report fails
  P105 main() prints and writes the report; PASS exits 0, FAIL exits 1
  P113 a cobertura report (the C library's and the C# lane's format, coverage.py's XML) yields the same
       figures as the JSON report: rates per class, missing lines, partial branches
  P133 an llvm-cov export (the C library's native report) yields lines, branches, regions and functions;
       --metrics chooses what is gated, a metric a format lacks fails, files match by trailing path,
       nothing-to-cover counts as 100 %
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
    assert text.startswith("COVERAGE GATE: FAIL (min 100% on lines, branches, 3 files)")
    assert "  alx/serial_logger.py: lines 89.5% < 100%" in text
    assert coverage_gate.main([str(report), "--min", "80", "alx/errors.py"]) == 0
    assert "COVERAGE GATE: PASS (min 80% on lines, branches, 1 files)" in capsys.readouterr().out


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


SPARSE_COBERTURA = """<?xml version="1.0" ?>
<coverage>
  <packages><package name="p"><classes>
    <class name="bare.py"><lines/></class>
    <class/>
  </classes></package></packages>
</coverage>
"""


def test_ALX1544_P130_sparse_entries_fall_back_safely_and_the_report_ends_with_a_newline(
    tmp_path, capsys
):
    """Mutation-driven hardening: missing attributes and keys take the documented defaults."""
    xml = tmp_path / "sparse.xml"
    xml.write_text(SPARSE_COBERTURA, encoding="utf-8")
    bare, unnamed = coverage_gate.load(xml)
    assert bare == FileCoverage("bare.py", 0.0, 100.0, (), 0)
    assert unnamed.name == "?"
    assert FileCoverage("a", 1.0, 1.0) == FileCoverage("a", 1.0, 1.0, (), 0)
    no_partial = {
        "files": {
            "m.py": {
                "summary": {
                    "num_statements": 1,
                    "covered_lines": 1,
                    "num_branches": 0,
                    "covered_branches": 0,
                }
            }
        }
    }
    assert coverage_gate.from_coverage_json(no_partial) == [
        FileCoverage("m.py", 100.0, 100.0, (), 0)
    ]
    assert coverage_gate.main([str(xml), "--min", "0"]) == 0
    assert capsys.readouterr().out.endswith("\n")


def llvm_file(name, **metrics):
    summary = {}
    for metric, (covered, count) in metrics.items():
        summary[metric] = {"count": count, "covered": covered, "percent": 0}
    return {"filename": name, "summary": summary}


LLVM = {
    "type": "llvm.coverage.json.export",
    "version": "3.1.0",
    "data": [
        {
            "files": [
                llvm_file(
                    "C:\\repo\\alxFifo.c",
                    lines=(120, 120),
                    branches=(38, 40),
                    regions=(200, 200),
                    functions=(9, 9),
                ),
                llvm_file(
                    "C:\\repo\\alxAssertPc.c",
                    lines=(10, 12),
                    branches=(0, 0),
                    regions=(7, 9),
                    functions=(3, 3),
                ),
            ],
            "totals": {},
        }
    ],
}


def test_ALX1544_P133_llvm_export_metrics_selection_and_trailing_path_match(tmp_path, capsys):
    report = tmp_path / "summary.json"
    report.write_text(json.dumps(LLVM), encoding="utf-8")
    assert_pc, fifo = sorted(coverage_gate.load(report), key=lambda f: f.name)
    assert (fifo.name, fifo.lines_pct, fifo.regions_pct, fifo.functions_pct) == (
        "C:/repo/alxFifo.c",
        100.0,
        100.0,
        100.0,
    )
    assert fifo.branches_pct == pytest.approx(95.0)
    assert assert_pc.branches_pct == 100.0, "no branches to cover = 100 %, not llvm's 0 %"
    assert assert_pc.functions_pct == 100.0

    # the C library's two calls: everything on the strict files, functions only on assert-guarded ones
    _, failures = coverage_gate.evaluate(
        [fifo, assert_pc], 100.0, ["alxFifo.c"], ("lines", "branches", "regions", "functions")
    )
    assert failures == ["C:/repo/alxFifo.c: branches 95.0% < 100%"]
    rows, failures = coverage_gate.evaluate(
        [fifo, assert_pc], 100.0, ["alxAssertPc.c"], ("functions",)
    )
    assert failures == []
    assert rows[0].startswith("C:/repo/alxAssertPc.c")
    assert "regions" in rows[0]

    # a metric the format lacks fails; a wanted file that is absent fails
    cob = coverage_gate.from_coverage_json(REPORT)
    _, failures = coverage_gate.evaluate(
        cob, 0.0, ["alx/errors.py", "nope.c"], ("lines", "functions")
    )
    assert failures == ["alx/errors.py: functions not in the report", "nope.c: not in the report"]
    # mutation-driven hardening: EVERY missing metric of a file is reported, not just the first.
    # A break after the first survived, because no test asked one file for two absent metrics.
    _, both = coverage_gate.evaluate(cob, 0.0, ["alx/errors.py"], ("functions", "regions"))
    assert both == [
        "alx/errors.py: functions not in the report",
        "alx/errors.py: regions not in the report",
    ]
    with pytest.raises(ValueError, match="unknown metric"):
        coverage_gate.evaluate(cob, 0.0, (), ("lines", "mcdc"))

    assert coverage_gate.main([str(report), "--metrics", "functions", "alxAssertPc.c"]) == 0
    assert (
        coverage_gate.main(
            [str(report), "--metrics", "lines,branches,regions,functions", "alxFifo.c"]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "COVERAGE GATE: PASS (min 100% on functions, 1 files)" in out
    assert "COVERAGE GATE: FAIL (min 100% on lines, branches, regions, functions, 1 files)" in out
