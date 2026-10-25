# SPDX-License-Identifier: MIT
"""alx.verify.gates.coverage - the per-file lines-and-branches gate over cobertura XML or coverage.py JSON.

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

Proofs (ALX-1564):
  P699 a pending file is reported under the minimum without failing; it fails once at the minimum on
       every gated metric, and a pending name the report does not carry fails as stale
  P706 an lcov tracefile yields lines, branches and functions from its records; lcov's exclusion
       markers in the source, each with a reason, take a line out of the figures or out of the
       branches alone, by line or by section, and are listed; a marker without a reason, a start
       without a stop, a stop without a start, and a stale marker - an excluded line the run reached,
       a branch-excluded line whose every branch was taken - fail
  P718 a line that is a closing brace alone is no line: llvm charges the brace after a return or
       a break with the region nothing reaches; a marked brace the run reached is stale all the same
  P808 the same exemptions live in a TOML file named by --exclusions, the sources free of comments:
       an entry names its file by trailing path and a regex over the line, scoped to a function and
       to the nth match when asked; kind lines, branches, block (the braces or the statements to the
       break or return after the matching line) and function (the body between the definition and
       the brace at column 0); each is listed under its file as a marker is
  P809 a stale or malformed exclusion fails: a match the source no longer holds, several matches
       without nth, nth beyond them, a function the source or the report lacks, an excluded line,
       block or function the run reached, a branch exclusion whose every branch was taken, entries
       for a source that cannot be read; an entry without file, kind, match or reason, or of an
       unknown kind, is refused when the file is read
"""

import json

import pytest

from alx.verify.gates import coverage as coverage_gate
from alx.verify.gates.coverage import FileCoverage


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


def test_ALX1564_P699_a_pending_file_is_reported_and_fails_only_once_it_is_at_the_minimum(
    tmp_path, capsys
):
    report = tmp_path / "summary.json"
    report.write_text(json.dumps(LLVM), encoding="utf-8")
    files = coverage_gate.load(report)
    assert_pc, fifo = sorted(f.name for f in files)
    every = ("lines", "branches", "regions", "functions")
    rows, failures = coverage_gate.evaluate(files, 100.0, (), every, ["alxAssertPc.c"])
    assert failures == [f"{fifo}: branches 95.0% < 100%"], "the queue does not fail"
    assert rows[0].startswith(assert_pc)
    assert rows[0].endswith("  (pending)")
    assert not rows[1].endswith("(pending)")
    _, failures = coverage_gate.evaluate(
        files, 100.0, ["alxAssertPc.c"], ("functions",), ["alxAssertPc.c"]
    )
    assert failures == [f"{assert_pc}: pending, and at the minimum - take it off the list"]
    _, failures = coverage_gate.evaluate(
        files, 100.0, ["alxFifo.c"], every, ["alxGone.c", "alxAssertPc.c"]
    )
    assert failures == [
        f"{fifo}: branches 95.0% < 100%",
        "alxGone.c: pending, and not in the report",
    ], "a pending file outside the wanted ones is still in the report"
    assert (
        coverage_gate.main(
            [str(report), "--metrics", "lines,regions", "--pending", "alxAssertPc.c"]
        )
        == 0
    )
    assert "(pending)" in capsys.readouterr().out


LCOV = """SF:SRC
FN:10,AlxX_Ctor
FNDA:3,AlxX_Ctor
FN:20,AlxX_Get
FNDA:0,AlxX_Get
DA:10,3
DA:11,3
DA:12,0
DA:13,0
DA:20,0
DA:21,0
DA:30,5
DA:31,2
DA:32,0
DA:33,0
DA:34,7
BRDA:11,0,0,3
BRDA:11,0,1,0
BRDA:30,0,0,5
BRDA:30,0,1,0
BRDA:31,0,0,2
BRDA:31,0,1,1
LF:11
LH:5
BRF:6
BRH:4
FNF:2
FNH:1
end_of_record
"""
SOURCE = """\
line 1
line 2
line 3
line 4
line 5
line 6
line 7
line 8
line 9
void AlxX_Ctor(void)
	if (a && b)                 // LCOV_EXCL_BR_LINE: the chain before leaves no other case
	{                           // LCOV_EXCL_START: the default of a switch over the enumeration
	ALX_X_ASSERT(false);
	}                           // LCOV_EXCL_STOP
line 15
line 16
line 17
line 18
line 19
void AlxX_Get(void)         /* LCOV_EXCL_LINE: a stub TV keeps */
	return 0;
line 22
line 23
line 24
line 25
line 26
line 27
line 28
line 29
	if (x)
	if (y)
	}
	};   // the brace after a return
	}   // LCOV_EXCL_LINE: not a brace rule - the marker; reached
"""


def test_ALX1564_P706_an_lcov_tracefile_is_gated_with_its_sources_exclusion_markers(
    tmp_path, capsys
):
    src = tmp_path / "alxX.c"
    src.write_text(SOURCE, encoding="ascii")
    info = tmp_path / "lcov.info"
    info.write_text(LCOV.replace("SRC", str(src)), encoding="ascii")

    (fc,) = coverage_gate.load(info)
    assert fc.name == str(src).replace("\\", "/")
    # 12 to 14 and 20 are excluded; the records left are 10, 11, 21, 30 and 31, and 21 is uncovered
    assert fc.missing_lines == (21,), "32 and 33 are braces alone, 34 is a marked brace"
    assert fc.lines_pct == pytest.approx(100.0 * 4 / 5)
    assert fc.branches_pct == pytest.approx(100.0 * 3 / 4), "11 is out; 30 half, 31 whole"
    assert fc.partial_branches == 1, "line 30; line 31 took both"
    assert fc.functions_pct == 100.0, "AlxX_Get at 20 is excluded, AlxX_Ctor ran"
    assert fc.exempt == (
        "line 11: LCOV_EXCL_BR_LINE: the chain before leaves no other case",
        "line 12: LCOV_EXCL_START: the default of a switch over the enumeration",
        "line 14: LCOV_EXCL_STOP: ?",
        "line 20: LCOV_EXCL_LINE: a stub TV keeps",
        "line 34: LCOV_EXCL_LINE: not a brace rule - the marker; reached",
    )
    assert fc.faults == (
        "line 14: LCOV_EXCL_STOP without a reason",
        "line 34: excluded, and the run reached it",
    ), "a marked brace the run reached is stale like any marked line"

    rows, failures = coverage_gate.evaluate([fc], 100.0, (), ("lines", "branches", "functions"))
    assert rows[1:] == [f"  exempt {e}" for e in fc.exempt]
    assert failures == [
        f"{fc.name}: line 14: LCOV_EXCL_STOP without a reason",
        f"{fc.name}: line 34: excluded, and the run reached it",
        f"{fc.name}: lines 80.0% < 100%",
        f"{fc.name}: branches 75.0% < 100%",
    ]

    stale = SOURCE.replace("line 1\n", "line 1  // LCOV_EXCL_LINE: reached\n").replace(
        "\tif (y)", "\tif (y)  // LCOV_EXCL_BR_LINE: both taken"
    )
    src.write_text(stale, encoding="ascii")
    (fc,) = coverage_gate.load(info)
    assert "line 1: excluded, and the run reached it" not in fc.faults, (
        "line 1 has no record: nothing reached"
    )
    assert "line 31: excluded from branches, and the run took every branch" in fc.faults

    broken = "// LCOV_EXCL_START: a\n// LCOV_EXCL_STOP: b\n// LCOV_EXCL_START: c\n// LCOV_EXCL_BR_STOP: d"
    excluded, branches, listed, faults = coverage_gate.exclusions(broken)
    assert (excluded, branches) == ({1, 3, 4}, set())
    assert faults == [
        "line 4: LCOV_EXCL_BR_STOP without a start",
        "line 3: LCOV_EXCL_START without a stop",
    ]
    assert len(listed) == 4
    nested = "// LCOV_EXCL_START: a\n// LCOV_EXCL_START: b\n// LCOV_EXCL_STOP: c"
    assert coverage_gate.exclusions(nested)[3] == [
        "line 2: LCOV_EXCL_START inside a section open since line 1"
    ]

    stray = "TN:\n" + LCOV.replace("SRC", str(src))
    (unread,) = coverage_gate.from_lcov(stray, lambda _path: None)
    assert unread.exempt == (), "a source that cannot be read exempts nothing"
    assert unread.lines_pct == pytest.approx(100.0 * 5 / 11), (
        "and every record counts, the braces too"
    )
    (gone,) = coverage_gate.from_lcov(LCOV.replace("SRC", str(tmp_path / "gone.c")))
    assert gone.exempt == (), "a source that is not there, the same"

    assert coverage_gate.main([str(info), "--metrics", "functions"]) == 1, "the stale marker fails"
    out = capsys.readouterr().out
    assert "  exempt line 20: LCOV_EXCL_LINE: a stub TV keeps" in out


C_LCOV = """SF:SRC
FN:3,AlxY_Ctor
FNDA:2,AlxY_Ctor
FN:19,AlxY_Read
FNDA:0,AlxY_Read
DA:3,2
DA:5,2
DA:6,2
DA:7,2
DA:9,2
DA:10,2
DA:11,2
DA:13,0
DA:14,0
DA:16,2
DA:19,0
DA:21,0
DA:22,0
DA:25,0
BRDA:5,0,0,2
BRDA:5,0,1,0
BRDA:9,0,0,2
BRDA:9,0,1,0
BRDA:16,0,0,2
BRDA:16,0,1,0
end_of_record
"""
C_SOURCE = """\
#include "alxY.h"

void AlxY_Ctor(AlxY* me, int kind)
{
	while (1)
	{
		if (kind == 1) break;
	}
	switch (kind)
	{
		case 1: me->a = 1; break;
		default:
			ALX_Y_ASSERT(false);
			break;
	}
	if (me->a && kind) return;
}

ALX_UNUSED static int AlxY_Read(AlxY* me)
{
	return me->a;
}

static void AlxY_Lone(void)
	return;
"""

EXCLUSIONS = """\
[[exclude]]
file = "alxY.c"
kind = "branches"
match = "while \\\\(1\\\\)"
reason = "a constant loop condition"

[[exclude]]
file = "alxY.c"
function = "AlxY_Ctor"
kind = "block"
match = "^\\\\s*default:$"
reason = "the enumeration"

[[exclude]]
file = "alxY.c"
kind = "lines"
match = "break;"
nth = 3
reason = "the break of the default"

[[exclude]]
file = "alxY.c"
kind = "function"
match = "AlxY_Read"
reason = "an uncalled reader"

[[exclude]]
file = "other.c"
kind = "lines"
match = "anything"
reason = "another file: not applied here"
"""


def test_ALX1564_P808_the_exclusions_of_a_toml_file_apply_to_an_lcov_tracefile(tmp_path, capsys):
    src = tmp_path / "alxY.c"
    src.write_text(C_SOURCE, encoding="ascii")
    info = tmp_path / "lcov.info"
    info.write_text(C_LCOV.replace("SRC", str(src)), encoding="ascii")
    toml = tmp_path / "coverage.toml"
    toml.write_text(EXCLUSIONS, encoding="ascii")

    (fc,) = coverage_gate.load(info, toml)
    # the default's block (12 to 14), the break (14) and AlxY_Read (19 to 22) are out; of the
    # records left (3, 5, 6, 7, 9, 10, 11, 16, 25) only 25 is uncovered
    assert fc.missing_lines == (25,)
    assert fc.lines_pct == pytest.approx(100.0 * 8 / 9)
    assert fc.branches_pct == pytest.approx(50.0), "line 5 is out; 9 and 16 half each"
    assert fc.functions_pct == 100.0, "AlxY_Read at 19 is excluded, AlxY_Ctor ran"
    assert fc.exempt == (
        "branches 'while \\\\(1\\\\)' (line 5): a constant loop condition",
        "block '^\\\\s*default:$' in AlxY_Ctor (lines 12, 13-14): the enumeration",
        "lines 'break;' (line 14): the break of the default",
        "function 'AlxY_Read' (lines 19-22): an uncalled reader",
    )
    assert fc.faults == ()

    rows, failures = coverage_gate.evaluate([fc], 100.0, (), ("lines", "branches", "functions"))
    assert rows[1:] == [f"  exempt {e}" for e in fc.exempt]
    assert failures == [
        f"{fc.name}: lines 88.9% < 100%",
        f"{fc.name}: branches 50.0% < 100%",
    ]
    assert coverage_gate.main([str(info), "--metrics", "functions", "--exclusions", str(toml)]) == 0
    out = capsys.readouterr().out
    assert "  exempt function 'AlxY_Read' (lines 19-22): an uncalled reader" in out
    assert coverage_gate.main([str(info), "--metrics", "functions"]) == 1, "without the file"

    # a block with braces: the switch; one without: the label up to its break; a one-line case is
    # its own block; the function runs to the end when no brace at column 0 closes it; the entries'
    # file is matched by its trailing path
    lines = C_SOURCE.split("\n")
    assert coverage_gate._block_range(lines, 9) == (10, 15)
    assert coverage_gate._block_range(lines, 12) == (13, 14)
    assert coverage_gate._block_range(lines, 11) == (11, 11)
    assert coverage_gate._function_range(lines, "AlxY_Lone") == (24, 26)
    assert coverage_gate._function_range(lines, "AlxY_Gone") is None
    assert coverage_gate._block_range(["if (x)", "", "{", "a;", "}"], 1) == (2, 5)
    assert coverage_gate._block_range(["if (x)", "{", "a;"], 1) == (2, 3), "unclosed: to the end"
    assert coverage_gate._block_range(["default:", "a;"], 1) == (2, 2), "no break: to the end"
    entries = coverage_gate.load_exclusions(toml)
    assert [e["file"] for e in entries] == ["alxY.c"] * 4 + ["other.c"]
    (same,) = coverage_gate.from_lcov(
        C_LCOV.replace("SRC", "C:\\repo\\Ext\\alxY.c"), lambda _p: C_SOURCE, entries
    )
    assert same.exempt == fc.exempt


def test_ALX1564_P809_a_stale_or_malformed_exclusion_fails(tmp_path):
    src = tmp_path / "alxY.c"
    src.write_text(C_SOURCE, encoding="ascii")
    lcov = C_LCOV.replace("SRC", str(src))

    def faults_of(text=lcov, source=C_SOURCE, file="alxY.c", **entry):
        entry.update(file=file, reason="r")
        (fc,) = coverage_gate.from_lcov(text, lambda _p: source, [entry])
        return list(fc.faults)

    assert faults_of(kind="lines", match="no such text") == [
        "lines 'no such text': matches nothing"
    ]
    assert faults_of(kind="block", match="break;") == ["block 'break;': 3 matches, give nth"]
    assert faults_of(kind="lines", match="break;", nth=4) == ["lines 'break;': nth 4, 3 match(es)"]
    assert faults_of(kind="lines", match="break;", function="AlxY_Gone") == [
        "lines 'break;' in AlxY_Gone: no function AlxY_Gone in the source"
    ]
    assert faults_of(kind="function", match="AlxY_Lone") == [
        "function 'AlxY_Lone': no function AlxY_Lone in the source and the report"
    ], "in the source, not in the report"
    assert faults_of(kind="function", match="AlxY_Ctor") == [
        "function 'AlxY_Ctor': excluded, and the run reached it"
    ]
    assert faults_of(kind="lines", match="switch") == [
        "lines 'switch' (line 9): excluded, and the run reached it"
    ]
    assert faults_of(kind="lines", match="switch", function="AlxY_Ctor") == [
        "lines 'switch' in AlxY_Ctor (line 9): excluded, and the run reached it"
    ], "scoped to the function, the same"
    half = lcov.replace("BRDA:16,0,1,0", "BRDA:16,0,1,1")
    assert faults_of(half, kind="branches", match="me->a && kind") == [
        "branches 'me->a && kind' (line 16): excluded from branches, and the run took every branch"
    ]
    assert faults_of(half, kind="block", match="me->a && kind") == [
        "block 'me->a && kind' (line 16): excluded from branches, and the run took every branch",
        "block 'me->a && kind' (lines 16-16): excluded, and the run reached it",
    ], "a one-line block is the line: reached, stale twice"
    assert faults_of(kind="block", match="^\\s*case 1:") == [
        "block '^\\\\s*case 1:' (lines 11-11): excluded, and the run reached it"
    ]
    assert faults_of(kind="block", match="while") == [
        "block 'while' (lines 6-8): excluded, and the run reached it"
    ]
    assert faults_of(source=None, kind="lines", match="x") == [
        "exclusions configured, and the source cannot be read"
    ]
    assert faults_of(source=None, file="other.c", kind="lines", match="x") == []

    toml = tmp_path / "coverage.toml"
    toml.write_text('[[exclude]]\nfile = "a.c"\nkind = "lines"\nmatch = "x"\n', encoding="ascii")
    with pytest.raises(ValueError, match="missing reason"):
        coverage_gate.load_exclusions(toml)
    toml.write_text(
        '[[exclude]]\nfile = "a.c"\nkind = "region"\nmatch = "x"\nreason = "r"\n', encoding="ascii"
    )
    with pytest.raises(ValueError, match="unknown kind, from lines, branches, block, function"):
        coverage_gate.load_exclusions(toml)
    toml.write_text("", encoding="ascii")
    assert coverage_gate.load_exclusions(toml) == []
