# SPDX-License-Identifier: MIT
"""Evidence interpretation: a passing process is not an all-passing test inventory."""

import pytest

from alx.verify.results import read_cases


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
