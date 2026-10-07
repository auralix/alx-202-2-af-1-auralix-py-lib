# SPDX-License-Identifier: MIT
"""alx.verify.gates.c_format: decision 12's table over the format strings of C sources.

The C in these tests is written as text, never compiled: the gate scans, so a fragment is enough.

Proofs (ALX-1564):
  P671 the table's own forms pass: PRI macros spliced into the text, uppercase PRIX hex with its
       width, %d %c %s %p %f %%, the flags - and 0, width digits and .precision, %.*s with (int)len
       on the call - found by counting the values, a * taking one more; a value the call does not
       have is the compiler's business
  P672 every conversion outside the table is a finding naming it: %u %x %X %o %i %e %g %a %n, a
       length modifier, the # flag, a * width, a * precision on anything but s, a %.*s whose length
       is not (int) on the call
  P673 in sscanf only SCN macros, %lf and a bounded %s; outside sscanf no SCN macro
  P674 a lowercase PRIx and PRIo are findings; a format that is not a literal is not checked
  P675 the calls are found wherever they stand - a module trace macro, AlxTrace_WriteFormat,
       sprintf, snprintf, sscanf - but not in a comment, a string or a macro's own #define
  P676 an exemption names a file and a call; main() exits 0 on PASS and 1 on FAIL, --out writes the
       report
"""

from alx.verify.gates import c_format


def _findings(tmp_path, source, name="alxX.c", exempt=()):
    path = tmp_path / name
    path.write_text(source, encoding="ascii")
    return c_format.scan(path, exempt)


def test_ALX1564_P671_the_tables_own_forms_pass(tmp_path):
    source = (
        "void f(void)\n{\n"
        '\tALX_FIFO_TRACE_INF("len %" PRIu32 " id 0x%02" PRIX8 " v %" PRId16, len, id, v);\n'
        '\tALX_FIFO_TRACE_ERR("%d %c %s %p %f %% done", status, c, str, ptr, value);\n'
        '\tsprintf(buff, "%-8s|%08" PRIX32 "|%.3f|%5d", name, addr, value, n);\n'
        '\tsnprintf(buff, len, "buff %.*s", (int)len, data);\n'
        '\tsprintf(buff, "%" PRIu32 " %.*s", a, (int)len, data);\n'
        "}\n"
    )
    assert _findings(tmp_path, source) == []
    assert c_format.check_format(['"%.*s'], "sprintf") == [], "no value to read: the compiler's"


def test_ALX1564_P672_a_conversion_outside_the_table_is_a_finding(tmp_path):
    source = (
        "void f(void)\n{\n"
        '\tALX_X_TRACE_INF("%u %x %X %o %i %e %g %a %n", a, b, c, d, e, f, g, h, &i);\n'
        '\tALX_X_TRACE_INF("%lu %hhd %zd", a, b, c);\n'
        '\tALX_X_TRACE_INF("%#x %*d %.*d % d %+d", a, w, b, p, c, d, e);\n'
        '\tALX_X_TRACE_INF("done 50%");\n'
        '\tsnprintf(b, n, "%" PRIu32 " %*d %.*s", a, w, d, len, s);\n'
        "}\n"
    )
    found = _findings(tmp_path, source)
    for spec in ("'%u'", "'%x'", "'%X'", "'%o'", "'%i'", "'%e'", "'%g'", "'%a'", "'%n'"):
        assert any(spec in f and "not in the table" in f for f in found), spec
    for spec in ("'%lu'", "'%hhd'", "'%zd'"):
        assert any(spec in f and "length modifier" in f for f in found), spec
    assert any("'%#x'" in f and "flags" in f for f in found)
    assert any("'%*d'" in f and "* width" in f for f in found)
    assert any("'%.*d'" in f and "only as %.*s" in f for f in found)
    assert any("'% d'" in f and "flags" in f for f in found)
    assert any("'%+d'" in f and "flags" in f for f in found)
    assert any(":6: " in f and "'%': a % with no conversion" in f for f in found)
    assert [f.split(": ", 2)[2] for f in found if ":7: " in f] == [
        "'%*d': a * width",
        "'%.*s': its length is (int)len on the call",
    ], "the length is the fourth value: one for the macro, two for %*d"
    assert not any("'%.*d'" in f and "(int)len" in f for f in found), "found once, as a * precision"
    assert all(f.startswith(str(tmp_path / "alxX.c")) for f in found)


def test_ALX1564_P673_sscanf_takes_scn_macros_lf_and_a_bounded_s(tmp_path):
    good = 'void f(void)\n{\n\tsscanf(s, "(%" SCNu8 ",%" SCNd32 ") %lf %31s", &a, &b, &c, d);\n}\n'
    assert _findings(tmp_path, good) == []
    bad = (
        "void f(void)\n{\n"
        '\tsscanf(s, "%s %" PRIu32, d, &a);\n'
        '\tsprintf(b, "%" SCNu32 " %lf", a, c);\n'
        "}\n"
    )
    found = _findings(tmp_path, bad)
    assert any("unbounded %s" in f for f in found)
    assert any("PRIu32" in f and "SCN macros in sscanf" in f for f in found)
    assert any("SCNu32" in f and "PRI macros in a print" in f for f in found)
    assert any("'%lf'" in f and "length modifier" in f for f in found), (
        "%lf is right in sscanf only"
    )


def test_ALX1564_P674_lowercase_prix_and_prio_are_findings_a_variable_format_is_not_checked(
    tmp_path,
):
    source = (
        "void f(void)\n{\n"
        '\tALX_X_TRACE_INF("%" PRIx32 " %" PRIo8, a, b);\n'
        "\tALX_X_TRACE_INF(fmt, a);\n"
        "}\n"
    )
    found = _findings(tmp_path, source)
    assert any("PRIx32" in f for f in found)
    assert any("PRIo8" in f for f in found)
    assert len(found) == 2, "the variable format is passed over"


def test_ALX1564_P675_calls_are_found_in_code_only(tmp_path):
    source = (
        "#define ALX_X_TRACE_INF(...) ALX_TRACE_INF(ALX_X_FILE, __VA_ARGS__)\n"
        "void f(void)\n{\n"
        '\t// ALX_X_TRACE_INF("%u", a);\n'
        '\t/* sprintf(b, "%u", a); */\n'
        '\tconst char* s = "sscanf(s, \\"%u\\")";\n'
        '\tFOO_MAIN_TRACE_WRN("%u", a);\n'
        '\tAlxTrace_WriteFormat(&alxTrace, "%u", a);\n'
        "}\n"
    )
    found = _findings(tmp_path, source)
    assert len(found) == 2, found
    assert any(":7: FOO_MAIN_TRACE_WRN:" in f for f in found), "any repository's trace macro"
    assert any(":8: AlxTrace_WriteFormat:" in f for f in found)
    cut = _findings(tmp_path, 'void f(void)\n{\n\tsprintf(b, "%u"', name="alxCut.c")
    assert len(cut) == 1, "a call cut off at the end of the file is still read"


def test_ALX1564_P676_an_exemption_names_a_file_and_a_call_and_main_reports(tmp_path, capsys):
    source = 'void f(void)\n{\n\tsscanf(s, "%lu%n", &a, &n);\n\tsprintf(b, "%u", a);\n}\n'
    path = tmp_path / "alxBoot.c"
    path.write_text(source, encoding="ascii")
    found = c_format.scan(path, ["alxBoot.c:sscanf"])
    assert len(found) == 1, "the sscanf is exempt, the sprintf is not"
    assert "sprintf" in found[0]

    out = tmp_path / "report" / "c_format.txt"
    assert c_format.main([str(path), "--exempt", "alxBoot.c:sscanf", "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert printed.startswith("C FORMAT GATE: FAIL")
    assert out.read_text(encoding="ascii") == printed

    clean = tmp_path / "alxClean.c"
    clean.write_text('void f(void)\n{\n\tsprintf(b, "%d", a);\n}\n', encoding="ascii")
    assert c_format.main([str(clean)]) == 0
