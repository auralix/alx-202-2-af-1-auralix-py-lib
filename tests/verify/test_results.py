# SPDX-License-Identifier: MIT
"""Evidence interpretation: a passing process is not an all-passing test inventory.

P117, P118 and P127 prove the functionality report; they moved here with their proof tokens from
the first consumer's framework suite, which keeps only its own map and command line.
"""

import json

import pytest

from alx.verify.results import read_cases, write_functionality_report

FUNCTIONALITIES = {"test_cli": "CLI commands", "test_boot": "Boot and identity"}


def _run(tmp_path, *reports):
    """A run folder with one pytest report per (mode, testcases) pair."""
    for mode, cases in reports:
        (tmp_path / mode).mkdir()
        (tmp_path / mode / "pytest_report.xml").write_text(
            f"<testsuite>{cases}</testsuite>", encoding="utf-8"
        )
    return tmp_path


@pytest.mark.parametrize(
    ("detail", "outcome"),
    [
        ("", "passed"),
        ('<failure message="wrong value"/>', "failed"),
        ('<error message="fixture unavailable"/>', "error"),
        ('<skipped type="pytest.skip" message="capability absent"/>', "skipped"),
        ('<skipped type="pytest.xfail" message="known defect"/>', "xfail"),
    ],
)
def test_ALX1564_P200_report_outcomes_preserve_failures_and_skips(tmp_path, detail, outcome):
    report = tmp_path / "report.xml"
    report.write_text(
        f'<testsuites><testsuite><testcase name="check" classname="test_protocol" time="0.25">{detail}</testcase></testsuite></testsuites>'
    )
    (case,) = read_cases(report)
    assert case.outcome == outcome
    assert case.seconds == 0.25
    assert case.module == "test_protocol"
    assert bool(case.reason) is (outcome != "passed")


def test_ALX1564_P201_report_preserves_multiple_requirement_links(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuite><testcase name="check"><properties><property name="req" value="R1"/><property name="req" value="R2"/></properties></testcase></testsuite>'
    )
    (case,) = read_cases(report)
    assert case.properties == (("req", "R1"), ("req", "R2"))


@pytest.mark.parametrize("xml", ["<testsuites/>", "<unrelated/>"])
def test_ALX1564_P202_empty_or_unrelated_report_is_not_success(tmp_path, xml):
    report = tmp_path / "report.xml"
    report.write_text(xml)
    with pytest.raises(ValueError, match=r"no test cases|not a JUnit report"):
        read_cases(report)


@pytest.mark.parametrize(
    ("details", "outcome", "reasons"),
    [
        (
            '<failure message="body failed"/><error message="cleanup failed"/>',
            "error",
            ["cleanup failed", "body failed"],
        ),
        (
            '<error message="first cleanup failed"/><error>second cleanup failed</error>',
            "error",
            ["first cleanup failed", "second cleanup failed"],
        ),
        (
            '<skipped type="pytest.xfail" message="known defect"/>'
            '<skipped type="pytest.xfail" message="known defect"/>',
            "xfail",
            ["known defect"],
        ),
        (
            '<skipped type="pytest.xfail" message="known defect"/>'
            '<error message="unexpected cleanup failure"/>',
            "error",
            ["unexpected cleanup failure", "known defect"],
        ),
    ],
)
def test_ALX1564_P204_multiple_phase_diagnostics_remain_visible(
    tmp_path, details, outcome, reasons
):
    report = tmp_path / "report.xml"
    report.write_text(f'<testsuite><testcase name="check">{details}</testcase></testsuite>')
    (case,) = read_cases(report)
    assert case.outcome == outcome
    assert case.reason.splitlines() == reasons


def test_ALX1564_P117_matrix_keeps_expected_failures_out_of_pass_counts(tmp_path):
    run = _run(
        tmp_path,
        (
            "api",
            '<testcase name="works" classname="tests.target.test_cli"/>'
            '<testcase name="defect" classname="tests.target.test_cli">'
            '<skipped type="pytest.xfail" message="known"/></testcase>',
        ),
    )
    assert write_functionality_report(run, ["api"], FUNCTIONALITIES, notes=["A caveat."])
    rows = json.loads((run / "functionality.json").read_text(encoding="utf-8"))
    assert [row["outcome"] for row in rows] == ["passed", "xfail"]
    assert {row["mode"] for row in rows} == {"api"}
    assert {row["execution_location"] for row in rows} == {"target"}
    markdown = (run / "functionality.md").read_text(encoding="utf-8")
    assert "| api | CLI commands | 1 | 1 | 0 | 0 | 0 |" in markdown
    assert "Boot and identity" not in markdown, "a functionality with no case has no row"
    assert "A caveat." in markdown, "the repository's own notes are kept"


def test_ALX1564_P118_matrix_refuses_mixed_host_results(tmp_path):
    run = _run(tmp_path, ("api", '<testcase name="check" classname="tests.host.app.test_main"/>'))
    with pytest.raises(ValueError, match="mixed host input"):
        write_functionality_report(run, ["api"], FUNCTIONALITIES)


def test_ALX1564_P127_matrix_preserves_all_diagnostics_within_one_record(tmp_path):
    run = _run(
        tmp_path,
        (
            "api",
            '<testcase name="defect" classname="tests.target.test_cli">'
            '<skipped type="pytest.xfail" message="known body defect"/>'
            '<error message="unexpected | cleanup failure"/></testcase>',
        ),
        ("cli", '<testcase name="works" classname="test_boot"/>'),
    )
    assert not write_functionality_report(run, ["api", "cli"], FUNCTIONALITIES)
    first, second = json.loads((run / "functionality.json").read_text(encoding="utf-8"))
    assert first["outcome"] == "error"
    assert first["reason"] == "unexpected | cleanup failure\nknown body defect"
    assert second["functionality"] == "Boot and identity", "a module without the package prefix"
    markdown = (run / "functionality.md").read_text(encoding="utf-8")
    assert "unexpected / cleanup failure known body defect" in markdown, "one table cell"
    assert "| api | CLI commands | 0 | 0 | 0 | 0 | 1 |" in markdown
