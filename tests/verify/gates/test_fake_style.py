# SPDX-License-Identifier: MIT
"""alx.verify.gates.fake_style: the convention every link-time fake of a C library module follows.

The C here is text, never compiled: the gate scans, so a fragment is enough. One clean fake of an
imaginary watchdog module is varied one rule at a time.

Proofs (ALX-1564):
  P321 a fake that follows the convention passes, with and without the header check
  P322 a file not named alx<Module>Fake.c is a finding, and nothing else is checked
  P323 a fake that does not include its own module header is a finding
  P324 a quoted include that is not a library header, or an angled one that is not standard C
  P325 an exported function that is neither the module's API nor this fake's control
  P326 a control that takes a void pointer handle
  P327 file-scope state without a reset is a finding; a reset may take what it needs; no state
       needs no reset
  P328 a family guard must be the module header's own condition, a missing header is a finding
  P329 check() turns a non-ASCII file into a finding; main() exits 0 on PASS, 1 on FAIL, --out
"""

from alx.verify.gates import fake_style

GUARD = "#if defined(ALX_STM32F4) || defined(ALX_STM32F7)"
CLEAN = f"""/**
  * @file\t\talxWdtFake.c
  **/
#include "alxWdt.h"
#include <string.h>

static uint32_t alxWdtFake_refreshCount;

void AlxWdtFake_Reset(void);
uint32_t AlxWdtFake_RefreshCount(void);

void AlxWdtFake_Reset(void)
{{
\talxWdtFake_refreshCount = 0;
}}

static uint32_t AlxWdtFake_Twice(uint32_t n)
{{
\treturn 2 * n;
}}

{GUARD}
void AlxWdt_Ctor(AlxWdt* me, AlxWdt_Config config)
{{
\t(void)me; (void)config;
}}
#endif

Alx_Status AlxWdt_Refresh(AlxWdt* me)
{{
\t(void)me;
\talxWdtFake_refreshCount++;
\treturn Alx_Ok;
}}
"""


def _headers(tmp_path, guard=GUARD):
    """A header folder holding the imaginary module's header, with its own family guard."""
    folder = tmp_path / "inc"
    folder.mkdir(parents=True)
    (folder / "alxWdt.h").write_text(f'{guard}\n#include "alxWdt_McuStm32.h"\n#endif\n')
    (folder / "alxWdt_McuStm32.h").write_text("#if defined(ALX_C_LIB) && defined(ALX_STM32F7)\n")
    return [folder]


def test_ALX1564_P321_a_fake_that_follows_the_convention_passes(tmp_path):
    assert fake_style.check_text("alxWdtFake.c", CLEAN) == []
    assert fake_style.check_text("alxWdtFake.c", CLEAN, _headers(tmp_path)) == []


def test_ALX1564_P322_a_file_not_named_after_its_module_is_a_finding():
    (finding,) = fake_style.check_text("wdtStub.c", CLEAN)
    assert finding == "wdtStub.c:1: not named alx<Module>Fake.c"


def test_ALX1564_P323_a_fake_that_does_not_include_its_module_header_is_a_finding():
    text = CLEAN.replace('#include "alxWdt.h"', '#include "alxGlobal.h"')
    assert fake_style.check_text("alxWdtFake.c", text) == [
        "alxWdtFake.c:1: does not include its module header alxWdt.h"
    ]


def test_ALX1564_P324_only_library_and_standard_headers_are_included():
    text = CLEAN.replace(
        "#include <string.h>", '#include "productHostFakes.h"\n#include <windows.h>'
    )
    assert fake_style.check_text("alxWdtFake.c", text) == [
        "alxWdtFake.c:5: includes productHostFakes.h, which is not a library header",
        "alxWdtFake.c:6: includes <windows.h>, which is not a C standard header",
    ]


def test_ALX1564_P325_every_exported_function_is_the_modules_api_or_this_fakes_control():
    text = CLEAN + "\nvoid AlxWdtOther_Ctor(AlxWdt* me)\n{\n}\nvoid Product_Record(void);\n"
    findings = fake_style.check_text("alxWdtFake.c", text)
    assert findings == [
        f"alxWdtFake.c:{CLEAN.count(chr(10)) + 2}: AlxWdtOther_Ctor is neither AlxWdt_ API nor an "
        "AlxWdtFake_ control",
    ], "a static helper is the fake's own business and a call is not a definition"


def test_ALX1564_P326_a_control_that_takes_a_void_pointer_handle_is_a_finding():
    text = CLEAN.replace(
        "uint32_t AlxWdtFake_RefreshCount(void);",
        "uint32_t AlxWdtFake_RefreshCount(const void* me);",
    )
    assert fake_style.check_text("alxWdtFake.c", text) == [
        "alxWdtFake.c:10: AlxWdtFake_RefreshCount takes a void pointer handle"
    ]


def test_ALX1564_P327_state_needs_a_reset_and_a_reset_may_take_what_it_needs():
    without = CLEAN.replace("void AlxWdtFake_Reset(void)", "void AlxWdtFake_Clear(void)").replace(
        "void AlxWdtFake_Reset(void);", "void AlxWdtFake_Clear(void);"
    )
    assert fake_style.check_text("alxWdtFake.c", without) == [
        "alxWdtFake.c:1: keeps file-scope state but defines no AlxWdtFake_Reset"
    ]
    stepped = CLEAN.replace("AlxWdtFake_Reset(void)", "AlxWdtFake_Reset(uint64_t step_ns)")
    assert fake_style.check_text("alxWdtFake.c", stepped) == []
    stateless = (
        '#include "alxWdt.h"\n\nAlx_Status AlxWdt_Init(AlxWdt* me)\n{\n\treturn Alx_Ok;\n}\n'
    )
    assert fake_style.check_text("alxWdtFake.c", stateless) == []


def test_ALX1564_P328_a_family_guard_must_be_the_module_headers_own(tmp_path):
    blanket = CLEAN.replace(GUARD, "#if defined(ALX_STM32)")
    assert fake_style.check_text("alxWdtFake.c", blanket) == [], "no header check without --headers"
    assert fake_style.check_text("alxWdtFake.c", blanket, _headers(tmp_path)) == [
        "alxWdtFake.c:22: family guard is not one of alxWdt.h's own: #if defined(ALX_STM32)"
    ]
    sub_header = CLEAN.replace(GUARD, "\t#if defined(ALX_C_LIB) && defined(ALX_STM32F7)")
    assert fake_style.check_text("alxWdtFake.c", sub_header, _headers(tmp_path / "b")) == [], (
        "an MCU sub-header's condition is the header's own too, indented or not"
    )
    assert fake_style.check_text("alxWdtFake.c", CLEAN, [tmp_path / "empty"]) == [
        "alxWdtFake.c:1: no header alxWdt.h in the given directories"
    ]


def test_ALX1564_P329_check_and_main(tmp_path, capsys):
    good = tmp_path / "alxWdtFake.c"
    good.write_text(CLEAN, encoding="ascii")
    bad = tmp_path / "alxAdcFake.c"
    bad.write_bytes(b'#include "alxAdc.h"\n/* caf\xc3\xa9 */\n')
    assert fake_style.check([good]) == []
    (finding,) = fake_style.check([bad], header_dirs=[])
    assert finding.startswith(f"{bad}:1: not ASCII")

    assert fake_style.main([str(good), "--headers", str(_headers(tmp_path)[0])]) == 0
    assert capsys.readouterr().out == "FAKE STYLE GATE: PASS (1 files)\n"
    out = tmp_path / "gate" / "fake_style.txt"
    assert fake_style.main([str(good), str(bad), "--out", str(out)]) == 1
    report = capsys.readouterr().out
    assert report.startswith("FAKE STYLE GATE: FAIL (1 finding(s))\n")
    assert out.read_text(encoding="ascii") == report
