# SPDX-License-Identifier: MIT
"""alx.verify.c_style: the two mechanical C rules - no ternary, aligned doxygen tag columns.

The C in these tests is written as text, never compiled: the gate scans, so a fragment is enough.
Tab stop 4 throughout, and every doc block is written with real tabs.

Proofs (ALX-1544):
  P149 a clean source produces no finding; the tag vocabulary is the doxygen one
  P150 a ternary in code is a finding with its line number
  P151 a '?' inside a line comment, a block comment, a string or a char literal is not a ternary
  P152 an escaped quote does not end a literal, so the '?' after it stays out of the code
  P153 a doc block whose name/value column or description column differs is a finding naming both
  P154 spaces instead of tabs between the fields of a tag line are a finding
  P155 lines that are not tag lines, unknown tags and empty tags are passed over
  P156 an unterminated doc block ends at the end of the file instead of running away
  P157 check() reads the files and turns a non-ASCII file into a finding, never a traceback
  P158 main(): exit 0 on PASS, 1 on FAIL, --out writes the same report as stdout
"""

from alx.verify import c_style

CLEAN = (
    "/**\n"
    "  ******************************************************************************\n"
    "  * @file\t\talxFifo.h\n"
    "  * @brief\t\tAuralix C Library - FIFO Module\n"
    "  ******************************************************************************\n"
    "  **/\n"
    "\n"
    # every name starts in column 20 and every description in column 28, tab stop 4
    "/**\n"
    "  * @brief\t\t\t\t\tWrite one byte into the FIFO\n"
    "  * @param[in,out]\tme\t\tPointer to the FIFO\n"
    "  * @param[out]\t\tdata\tThe byte\n"
    "  * @param[in]\t\tlen\t\tHow many\n"
    "  * @retval\t\t\tAlx_Ok\tByte written\n"
    "  * @return\t\t\t\t\tStatus\n"
    "  **/\n"
    "Alx_Status AlxFifo_Write(AlxFifo* me, uint8_t data, uint32_t len)\n"
    "{\n"
    "\tif (me->count == me->len)\n"
    "\t{\n"
    "\t\treturn Alx_ErrOutOfRange;\n"
    "\t}\n"
    "\telse\n"
    "\t{\n"
    "\t\treturn Alx_Ok;\n"
    "\t}\n"
    "}\n"
)


def test_ALX1544_P149_a_clean_source_has_no_findings_and_the_tag_vocabulary_is_doxygen():
    assert c_style.check_text("alxFifo.h", CLEAN) == []
    assert sorted(c_style.DESC_ONLY_TAGS) == ["brief", "details", "note", "return"]
    assert sorted(c_style.NAMED_TAGS) == ["param", "retval"]
    assert c_style.TABSTOP == 4


def test_ALX1544_P150_a_ternary_in_code_is_a_finding_with_its_line_number():
    text = "int a = 1;\nint b = a ? 2 : 3;\nint c = 4;\nint d = c ? 5 : 6;\n"
    assert c_style.find_ternaries(text) == [2, 4]
    findings = c_style.check_text("x.c", text)
    assert findings == [
        "x.c:2: ternary operator (write if/else)",
        "x.c:4: ternary operator (write if/else)",
    ]


def test_ALX1544_P151_a_question_mark_in_a_comment_or_a_literal_is_not_a_ternary():
    text = (
        "int a = b / c;\n"  # a bare slash is division, not a comment
        "// is this a ternary? no\n"
        "int d = 1;\n"
        "/* neither ? is this\n"
        "   nor ? this */\n"
        'const char* s = "why? because";\n'
        "char q = '?';\n"
        "int real = d ? 1 : 0;\n"
    )
    assert c_style.find_ternaries(text) == [8]


def test_ALX1544_P152_an_escaped_quote_does_not_end_a_literal():
    text = 'const char* s = "he said \\"why?\\" loudly";\nchar c = \'\\\'\';\nint x = 1;\n'
    assert c_style.find_ternaries(text) == []
    assert c_style.find_ternaries("int y = z ? 1 : 0;  // done\n") == [1]


def test_ALX1544_P153_misaligned_name_and_description_columns_are_findings():
    text = "/**\n  * @param[in]\tme\tThe object\n  * @param[in]\t\tdata\tThe byte\n  **/\n"
    findings = c_style.check_text("x.h", text)
    assert len(findings) == 2
    assert findings[0].startswith("x.h:1: doc block name/value columns not aligned:")
    assert findings[1].startswith("x.h:1: doc block description columns not aligned:")
    assert "line(s) [2]" in findings[0]
    assert "line(s) [3]" in findings[0]
    # the same block written with one column each is clean
    aligned = "/**\n  * @param[in]\tme\t\tThe object\n  * @param[in]\tdata\tThe byte\n  **/\n"
    assert c_style.check_text("x.h", aligned) == []


def test_ALX1544_P154_spaces_between_the_fields_of_a_tag_line_are_findings():
    after_tag = "/**\n  * @brief Description behind a space\n  **/\n"
    assert c_style.check_text("x.h", after_tag) == [
        "x.h:2: spaces in field separator after @brief (tabs only)"
    ]
    after_name = "/**\n  * @param[in]\tme Description behind a space\n  **/\n"
    assert c_style.check_text("x.h", after_name) == [
        "x.h:2: spaces in field separator after 'me' (tabs only)"
    ]


def test_ALX1544_P155_non_tag_lines_unknown_tags_and_empty_tags_are_passed_over():
    text = (
        "/**\n"
        "  ****************************************\n"  # a separator line, no tag
        "  * @file\t\talxFifo.h\n"  # a tag this gate does not measure
        "  * @brief\n"  # a tag with nothing after it
        "  * @param[in]\tme\n"  # a name with no description
        "  * @param[in]\tdata\n"
        "  **/\n"
    )
    assert c_style.check_text("x.h", text) == []
    assert c_style.check_text("x.h", "int a = 1;\n") == []


def test_ALX1544_P156_an_unterminated_doc_block_ends_at_the_end_of_the_file():
    text = "/**\n  * @param[in]\tme\tOne\n  * @param[in]\t\tdata\tTwo\n"
    findings = c_style.check_text("x.h", text)
    assert len(findings) == 2
    assert all("not aligned" in f for f in findings)


def test_ALX1544_P157_check_reads_the_files_and_reports_a_non_ascii_file_as_a_finding(tmp_path):
    good = tmp_path / "good.c"
    good.write_text("int a = 1;\n", encoding="ascii")
    bad = tmp_path / "bad.c"
    bad.write_bytes(b"/* 25 \xb0C */\nint b = 2;\n")
    ternary = tmp_path / "ternary.c"
    ternary.write_text("int c = d ? 1 : 0;\n", encoding="ascii")

    findings = c_style.check([good, bad, ternary])
    assert len(findings) == 2
    assert findings[0] == f"{bad}:1: not ASCII (ordinal not in range(128) at byte 6)"
    assert findings[1] == f"{ternary}:1: ternary operator (write if/else)"
    assert c_style.check([good]) == []


def test_ALX1544_P158_main_reports_pass_and_fail_and_writes_the_report(tmp_path, capsys):
    good = tmp_path / "good.c"
    good.write_text("int a = 1;\n", encoding="ascii")
    bad = tmp_path / "bad.c"
    bad.write_text("int b = a ? 1 : 0;\n", encoding="ascii")

    assert c_style.main([str(good)]) == 0
    assert capsys.readouterr().out == "C STYLE GATE: PASS (1 files)\n"

    out = tmp_path / "reports" / "c_style.txt"
    assert c_style.main([str(good), str(bad), "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert printed.splitlines()[0] == "C STYLE GATE: FAIL (1 finding(s))"
    assert "ternary operator (write if/else)" in printed
    assert out.read_text(encoding="ascii") == printed
