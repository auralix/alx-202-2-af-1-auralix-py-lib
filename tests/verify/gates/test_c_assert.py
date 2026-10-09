# SPDX-License-Identifier: MIT
"""alx.verify.gates.c_assert: decision 14, an assertion or a trace evaluates no call.

The C in these tests is written as text, never compiled: the gate scans, so a fragment is enough.

Proofs (ALX-1564):
  P686 a call inside an assertion or a trace is a finding naming the macro and the call, whatever
       the repository's prefix, a call inside a cast or a call through a pointer included
  P687 variables, comparisons, casts, sizeof and macros spelled in capitals are no calls; a call in
       a comment, a literal or a macro's own #define line is not read
  P688 main() exits 0 on PASS and 1 on FAIL, and --out writes the report
"""

from alx.verify.gates import c_assert


def _findings(tmp_path, source, name="alxX.c"):
    path = tmp_path / name
    path.write_text(source, encoding="ascii")
    return c_assert.scan(path)


def test_ALX1564_P686_a_call_inside_an_assertion_or_a_trace_is_a_finding(tmp_path):
    source = (
        "void f(AlxX* me)\n{\n"
        "\tALX_X_ASSERT(AlxX_IsClkOk(me));\n"
        "\tALX_X_ASSERT(strlen(key) < 8);\n"
        '\tALX_X_TRACE_WRN("len %" PRIu32, (uint32_t)AlxX_GetLen(me));\n'
        "\tFOO_MAIN_ASSERT(me->cb(me) == 0);\n"
        "}\n"
    )
    found = _findings(tmp_path, source)
    assert [f.split(": ", 1)[1] for f in found] == [
        "ALX_X_ASSERT: a call: AlxX_IsClkOk()",
        "ALX_X_ASSERT: a call: strlen()",
        "ALX_X_TRACE_WRN: a call: AlxX_GetLen()",
        "FOO_MAIN_ASSERT: a call: cb()",
    ]
    assert found[0].startswith(f"{tmp_path / 'alxX.c'}:3: ")


def test_ALX1564_P687_variables_casts_sizeof_and_capital_macros_are_no_calls(tmp_path):
    source = (
        "#define ALX_X_ASSERT(expr) ALX_ASSERT_RST(ALX_X_FILE, expr)\n"
        "void f(AlxX* me)\n{\n"
        "\tALX_X_ASSERT(isClkOk);\n"
        "\tALX_X_ASSERT((me->len <= ALX_ARR_LEN(me->buff)) && (sizeof(me->buff) > 0));\n"
        "\tALX_X_ASSERT((uint32_t)(len) < 8);\n"
        '\tALX_X_TRACE_INF("%s(%d)", "AlxX_Name()", (int32_t)me->id);\n'
        "\t// ALX_X_ASSERT(AlxX_IsClkOk(me));\n"
        '\tconst char* s = "ALX_X_ASSERT(AlxX_IsClkOk(me))";\n'
        "}\n"
    )
    assert _findings(tmp_path, source) == []


def test_ALX1564_P688_main_reports_pass_and_fail(tmp_path, capsys):
    bad = tmp_path / "alxBad.c"
    bad.write_text("void f(void)\n{\n\tALX_X_ASSERT(Check());\n}\n", encoding="ascii")
    good = tmp_path / "alxGood.c"
    good.write_text("void f(void)\n{\n\tALX_X_ASSERT(ok);\n}\n", encoding="ascii")

    out = tmp_path / "report" / "c_assert.txt"
    assert c_assert.main([str(bad), str(good), "--out", str(out)]) == 1
    printed = capsys.readouterr().out
    assert printed.startswith("C ASSERT GATE: FAIL (2 files)")
    assert out.read_text(encoding="ascii") == printed

    assert c_assert.main([str(good)]) == 0
    assert capsys.readouterr().out == "C ASSERT GATE: PASS (1 files)\n"
