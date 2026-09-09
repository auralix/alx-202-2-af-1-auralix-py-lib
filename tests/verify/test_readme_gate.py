# SPDX-License-Identifier: MIT
"""alx.verify.readme_gate: Markdown headings #, ##, #### only; no horizontal rules or setext underlines."""

from alx.verify import readme_gate

GOOD = "# Title\n\ntext\n\n## Chapter\n\n#### Sub-chapter\n\n- bullet\n"


def test_ALX1544_P142_allowed_levels_are_title_chapter_subchapter_and_a_good_file_has_no_findings():
    assert sorted(readme_gate.ALLOWED_LEVELS) == [1, 2, 4]
    assert readme_gate.check_text(GOOD) == []


def test_ALX1544_P143_other_heading_levels_are_findings_with_line_numbers():
    text = "# T\n\n### three\n\n##### five\n\n###### six\n"
    findings = readme_gate.check_text(text)
    assert [line for line, _ in findings] == [3, 5, 7]
    assert all("#, ##, #### only" in what for _, what in findings)
    assert findings[0][1].startswith("heading level 3")
    assert readme_gate.check_text("#hashtag is not a heading\n") == []


def test_ALX1544_P144_horizontal_rules_and_setext_underlines_are_findings():
    text = "# T\n\n---\n\n***\n\n_ _ _\n\nChapter\n===\n"
    findings = readme_gate.check_text(text)
    assert [line for line, _ in findings] == [3, 5, 7, 10]
    assert "horizontal rule" in findings[0][1]
    assert "setext" in findings[3][1]


def test_ALX1544_P145_fenced_code_blocks_are_not_inspected():
    text = "# T\n\n```\n### not a heading\n---\n```\n\n~~~\n##### neither\n~~~\n\n## C\n"
    assert readme_gate.check_text(text) == []


def test_ALX1544_P146_markdown_files_skip_tool_folders_and_honour_exclude(tmp_path):
    (tmp_path / "README.md").write_text(GOOD, encoding="ascii")
    (tmp_path / "Doc").mkdir()
    (tmp_path / "Doc" / "guide.markdown").write_text(GOOD, encoding="ascii")
    (tmp_path / "Ext").mkdir()
    (tmp_path / "Ext" / "vendor.md").write_text("### x\n", encoding="ascii")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "report.md").write_text("### x\n", encoding="ascii")
    (tmp_path / "notes.txt").write_text("### not markdown\n", encoding="ascii")
    files = readme_gate.markdown_files(tmp_path, exclude=["Ext"])
    assert [p.relative_to(tmp_path).as_posix() for p in files] == [
        "Doc/guide.markdown",
        "README.md",
    ]
    assert readme_gate.check(files) == []


def test_ALX1544_P147_cli_reports_pass_or_fail_writes_the_report_and_names_file_and_line(
    tmp_path, capsys
):
    (tmp_path / "README.md").write_text(GOOD, encoding="ascii")
    out = tmp_path / "build" / "analyze" / "readme_gate.txt"
    assert readme_gate.main([str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text(encoding="ascii").startswith("README GATE: PASS (1 files)")
    bad = tmp_path / "Doc"
    bad.mkdir()
    (bad / "bad.md").write_text("# T\n\n### no\n", encoding="ascii")
    assert readme_gate.main([str(tmp_path), "--exclude", "nothing"]) == 1
    captured = capsys.readouterr().out
    assert "README GATE: FAIL (2 files, excluded: nothing)" in captured
    assert "bad.md:3: heading level 3" in captured
