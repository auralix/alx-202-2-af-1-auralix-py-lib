# SPDX-License-Identifier: MIT
"""alx.verify.lanes: the lane vocabulary every noxfile shares (stages, evidence folders, reports, tools)."""

import pytest

from alx.verify import lanes


def test_ALX1544_P137_stages_are_the_pipeline_order_and_the_default_set_omits_only_mutate():
    assert lanes.STAGES == ("build", "test", "analyze", "sanitize", "coverage", "mutate")
    assert lanes.DEFAULT_SESSIONS == ("build", "test", "analyze", "sanitize", "coverage")
    assert lanes.BUILD_PRODUCT == "dist"


def test_ALX1544_P138_evidence_dir_creates_build_lane_and_parts_and_is_idempotent(tmp_path):
    out = lanes.evidence_dir(tmp_path, "matrix", "py312")
    assert out == tmp_path / "build" / "matrix" / "py312"
    assert out.is_dir()
    assert lanes.evidence_dir(tmp_path, "matrix", "py312") == out
    assert lanes.evidence_dir(tmp_path, "coverage") == tmp_path / "build" / "coverage"
    # mutation-driven hardening: EVERY part, not just the first. `for part in parts` with a break
    # after the first survived the suite, because no test passed more than one part.
    deep = lanes.evidence_dir(tmp_path, "sanitize", "asan", "smoke")
    assert deep == tmp_path / "build" / "sanitize" / "asan" / "smoke"
    assert deep.is_dir()


def test_ALX1544_P139_pytest_reports_put_junit_and_html_into_the_folder_as_posix_paths(tmp_path):
    out = tmp_path / "build" / "sanitize"
    opts = lanes.pytest_reports(out)
    assert opts == [
        f"--junitxml={(out / 'pytest_report.xml').as_posix()}",
        f"--html={(out / 'pytest_report.html').as_posix()}",
        "--self-contained-html",
    ]
    assert "\\" not in opts[0] + opts[1]


def test_ALX1544_P140_tool_takes_the_environment_variable_else_the_default(tmp_path, monkeypatch):
    default = tmp_path / "default.exe"
    default.write_bytes(b"")
    override = tmp_path / "override.exe"
    override.write_bytes(b"")
    monkeypatch.delenv("ALX_TEST_TOOL", raising=False)
    assert lanes.tool("ALX_TEST_TOOL", default) == default
    monkeypatch.setenv("ALX_TEST_TOOL", str(override))
    assert lanes.tool("ALX_TEST_TOOL", default) == override
    monkeypatch.setenv("ALX_TEST_TOOL", "")  # empty counts as unset
    assert lanes.tool("ALX_TEST_TOOL", str(default)) == default


def test_ALX1544_P141_tool_missing_raises_naming_the_variable_never_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("ALX_TEST_TOOL", raising=False)
    with pytest.raises(FileNotFoundError, match="ALX_TEST_TOOL"):
        lanes.tool("ALX_TEST_TOOL", tmp_path / "nowhere")
