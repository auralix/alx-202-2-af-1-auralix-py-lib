# SPDX-License-Identifier: MIT
"""alx.verify.configs: the configuration matrix, over a scripted compiler.

No compiler runs here, as in the host_build tests: the argv builders are pure functions and the
run goes through a scripted ``runner`` that answers the three questions a row asks of a compiler -
does this source compile, which headers does it lack, what does the preprocessor emit - from the
row's own configuration header, the way a real one would. The real compilers run in the C
repositories' ``matrix`` lanes.

Proofs (ALX-1564):
  P356 a matrix file holds rows, platforms, exemptions and what holds for every row; a platform's
       include folders and the shared ones are relative to the verification root
  P357 every rule of the file is enforced and named: the keys of a table, a row's id, words and
       description, an id allocated twice, a platform's word and host flag, the coverage table
  P358 a row's name is its id and its five words in axis order; rows are selected by id or name
  P359 a fragment is define, undef, include and comment lines; an include cycle and any other line
       are errors; the fragments the rows name and the repository lacks are listed once each
  P360 a row's header is the base with the fragments applied in axis order: a define activates
       the first line of its name (the value replaced, the comment kept), an undef comments every
       line out, an undeclared name is an error or, asked for, appended inside the include guard
  P361 the argv of a source: the mode, host or target flags, the defines, the include folders in
       their order (configuration, repository, shared, platform)
  P362 compiler output becomes findings, the missing headers of both compilers' wording and the
       undeclared identifiers; a dependency rule names the headers no folder holds
  P363 the conditional structure of a source: its branches, their nesting and guards, its code
       lines - comments, directives and their continuations are not code
  P364 coverage is by branch: a branch with any emitted line was compiled, its parents with it;
       uncovered code lines run together under the directive that hid them; a file no row
       reached is one run; an exemption explains a run by file pattern and guard identifier
  P365 a run: every row's configuration folder, its findings and verdict (PASS, FAIL, NO SHIM,
       the repository's own undeclared names a FAIL), the dependency pass only when a header is
       missing, coverage from the measured rows only, report.json and report.md
  P366 a run refuses a word it cannot give a meaning and a selection that matches no row
  P369 a missing member is named with the structure it is missing from, so one of the
       repository's own structures makes it the repository's finding (FAIL), a vendor's the
       fake's (NO SHIM); a structure without a name leaves the member alone
  P370 a repository's further configuration headers are written into every row's folder, each
       with the row's operations on the names it declares; the base takes the rest, an
       undeclared name an error there or, asked for, appended
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from alx.verify import configs as cf
from alx.verify.configs import MatrixError

BASE = """#ifndef CFG_H
#define CFG_H
//#define FEAT
#define LEVEL 1 // the level
//#define EXAMPLE "one"
//#define EXAMPLE "two"
#endif
"""

LIB_A = """#include "cfg.h"
int a(void)
{
#if defined(FEAT)
\treturn 1;
#else
\treturn 0;
#endif
}
"""

LIB_C = """#include "vendor.h"
int c(void) { return 2; }
"""

LIB_H = """#ifndef LIB_H
#define LIB_H
#ifdef __cplusplus
extern "C" {
#endif
int a(void);
#ifdef __cplusplus
}
#endif
#endif
"""

UNUSED_H = """/* never included */
int nobody(void);
"""

MATRIX = """
defines = ["SHARED=1"]
includes = ["shim/feature"]
coverage = { library = ["all"] }

[platform.p]
description = "the platform"
includes = ["shim/p"]
defines = ["PART"]
flags = ["-mthumb"]

[platform.host]
description = "the PC"
host = true

[[exempt]]
file = "lib/*.h"
guard = "__cplusplus"
why = "C++ linkage"

[[exempt]]
file = "lib/unused.h"
guard = ""
why = "a template nobody includes"

[[row]]
id = "0001"
toolchain = "cc"
platform = "p"
features = "on"
target = "t"
library = "all"
description = "feature on"

[[row]]
id = "0002"
toolchain = "cc"
platform = "p"
features = "off"
target = "t"
library = "all"
description = "feature off"

[[row]]
id = "0003"
toolchain = "cc"
platform = "host"
features = "off"
target = "t"
library = "elided"
description = "elided on the host"
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii", newline="\n")
    return path


@pytest.fixture
def repo(tmp_path):
    """A repository: a base header, five axes of fragments, two sources, two headers, a matrix."""
    root = tmp_path / "repo"
    _write(root / "base.h", BASE)
    _write(root / "lib" / "a.c", LIB_A)
    _write(root / "lib" / "c.c", LIB_C)
    _write(root / "lib" / "lib.h", LIB_H)
    _write(root / "lib" / "unused.h", UNUSED_H)
    frag = root / "frag"
    _write(frag / "toolchain" / "cc.h", "// the compiler; nothing changes\n")
    _write(frag / "platform" / "p.h", "")
    _write(frag / "platform" / "host.h", "#undef LEVEL\n")
    _write(frag / "features" / "on.h", '#include "common.h"\n#define FEAT\n')
    _write(frag / "features" / "common.h", "#define LEVEL 3\n")
    _write(frag / "features" / "off.h", "#undef FEAT\n")
    _write(frag / "target" / "t.h", "\n")
    _write(frag / "library" / "all.h", "#define EXAMPLE\n")
    _write(frag / "library" / "elided.h", "#undef EXAMPLE\n")
    _write(root / "matrix.toml", MATRIX)
    return root


def _config_of(argv) -> str:
    """The configuration header a scripted compiler would read: the first -I folder's cfg.h."""
    folder = next(a[2:] for a in argv if a.startswith("-I"))
    return (Path(folder) / "cfg.h").read_text(encoding="ascii")


class ScriptedCompiler:
    """Answers --version, -fsyntax-only, -E -M -MG and -E for the sources of the repository."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[list[str]] = []

    def __call__(self, argv, env=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "cc 9.9.9\nmore\n", "")
        source = Path(argv[-1])
        config = _config_of(argv)
        feat = "\n#define FEAT" in config
        elided = '//#define EXAMPLE "one"' in config
        if "-M" in argv:
            existing = self.root / "lib" / "lib.h"
            rule = f"c.o: {source} \\\n {existing} vendor.h deep/also\\ missing.h\n"
            return subprocess.CompletedProcess(argv, 0, rule, "")
        if "-E" in argv:
            if source.name == "c.c":
                return subprocess.CompletedProcess(argv, 1, "", "")
            body = "\nint a(void)\n{\n\n" + (
                "\treturn 1;\n\n\n\n" if feat else "\n\n\treturn 0;\n\n"
            )
            header = (self.root / "lib" / "lib.h").as_posix()
            text = (
                f'# 1 "{source.as_posix()}"\n{body}# 6 "{header}" 1\nint a(void);\n'
                '# 1 "X:\\\\outside\\\\x.h" 1\nint x;\n'
            )
            return subprocess.CompletedProcess(argv, 0, text, "")
        if source.name == "c.c":
            err = f"{source}:1:10: fatal error: vendor.h: No such file or directory\n"
            return subprocess.CompletedProcess(argv, 1, "", err)
        if elided:
            err = (
                f"{source}:7:9: warning: unused variable 'x' [-Wunused-variable]\n"
                f"{source}:7:9: note: declared here\n"
            )
            return subprocess.CompletedProcess(argv, 1, "", err)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _matrix(root: Path, **kw) -> cf.Matrix:
    compilers = {"cc": cf.Compiler("cc", Path("cc.exe"), target_flags=("--target=arm",))}
    args: dict[str, Any] = {
        "root": root,
        "base": root / "base.h",
        "fragments": root / "frag",
        "matrix": cf.load_matrix(root / "matrix.toml", root),
        "sources": [root / "lib" / "a.c", root / "lib" / "c.c"],
        "measured": [root / "lib" / n for n in ("a.c", "c.c", "lib.h", "unused.h")],
        "includes": [root / "lib"],
        "compilers": compilers,
        "aliases": {"cfg_usr.h": root / "lib" / "lib.h"},
        "config_name": "cfg.h",
    }
    args.update(kw)
    return cf.Matrix(**args)


# -- the matrix file -----------------------------------------------------------------------------
def test_ALX1564_P356_a_matrix_file_holds_rows_platforms_exemptions_and_shared_settings(repo):
    m = cf.load_matrix(repo / "matrix.toml", repo)
    assert [r.id for r in m.rows] == ["0001", "0002", "0003"]
    assert m.rows[0].words == {
        "toolchain": "cc", "platform": "p", "features": "on", "target": "t", "library": "all"
    }  # fmt: skip
    p = m.platforms["p"]
    assert (p.includes, p.defines, p.flags, p.host) == (
        (repo / "shim/p",),
        ("PART",),
        ("-mthumb",),
        False,
    )
    assert m.platforms["host"].host is True
    assert m.platforms["host"].includes == ()
    assert m.defines == ("SHARED=1",)
    assert m.includes == (repo / "shim/feature",)
    assert m.coverage == {"library": frozenset({"all"})}
    assert [e.guard for e in m.exemptions] == ["__cplusplus", ""]
    assert m.measures(m.rows[0])
    assert not m.measures(m.rows[2])
    # every top-level key is optional, and no coverage table measures every row
    empty = cf.parse_matrix("", repo)
    assert (empty.rows, empty.platforms, empty.exemptions, empty.defines) == ((), {}, (), ())
    assert empty.measures(m.rows[2])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("colour = 1", "unknown top-level key"),
        ('[[row]]\nid = "0001"', "missing toolchain"),
        ("row = [1]", "is not a table"),
        (
            '[[row]]\nid = "1"\ntoolchain = "a"\nplatform = "b"\nfeatures = "c"\ntarget = "d"\n'
            'library = "e"\ndescription = "f"',
            "is not 4 to 6 digits",
        ),
        (
            '[[row]]\nid = "0001"\ntoolchain = "A"\nplatform = "b-"\nfeatures = "c"\ntarget = "d"\n'
            'library = "e"\ndescription = "f"',
            "toolchain='A', platform='b-'",
        ),
        (
            '[[row]]\nid = "0001"\ntoolchain = "a"\nplatform = "b"\nfeatures = "c"\ntarget = "d"\n'
            'library = "e"\ndescription = " "',
            "description is empty",
        ),
        (
            '[[row]]\nid = "0001"\ntoolchain = "a"\nplatform = "b"\nfeatures = "c"\ntarget = "d"\n'
            'library = "e"\ndescription = "f"\ncolour = "g"',
            "unknown key",
        ),
        (
            '[[row]]\nid = "0001"\ntoolchain = "a"\nplatform = "b"\nfeatures = "c"\ntarget = "d"\n'
            'library = "e"\ndescription = "f"\n[[row]]\nid = "0001"\ntoolchain = "a"\nplatform = "b"\n'
            'features = "c"\ntarget = "d"\nlibrary = "e"\ndescription = "f"',
            "allocated twice",
        ),
        ('[platform.B]\ndescription = "x"', "a word is lowercase"),
        ('[platform.b]\ndescription = "x"\nhost = "yes"', "host is true or false"),
        ('[platform.b]\ndescription = "x"\nincludes = "one"', "includes is a list of strings"),
        ('[platform.b]\ndescription = "x"\nflags = [1]', "flags is a list of strings"),
        ("coverage = 1", "coverage is a table of axes"),
        ("[coverage]\ncolour = []", "coverage is a table of axes"),
        ('[[exempt]]\nfile = "*"', "missing guard, why"),
    ],
)
def test_ALX1564_P357_every_rule_of_the_matrix_file_is_enforced_and_named(tmp_path, text, message):
    with pytest.raises(MatrixError, match=re.escape(message)):
        cf.parse_matrix(text, tmp_path)


def test_ALX1564_P358_a_row_is_named_by_its_id_and_words_and_selected_by_either(repo):
    rows = cf.load_matrix(repo / "matrix.toml", repo).rows
    assert rows[0].name == "0001_cc_p_on_t_all"
    assert cf.select(rows, []) == list(rows)
    assert [r.id for r in cf.select(rows, ["0002"])] == ["0002"]
    assert [r.id for r in cf.select(rows, ["0003_cc_host", "0001"])] == ["0001", "0003"]
    assert cf.select(rows, ["9"]) == []


# -- fragments and the synthesized header ----------------------------------------------------------
def test_ALX1564_P359_fragments_are_define_undef_include_and_comment_lines(repo, tmp_path):
    frag = repo / "frag"
    assert cf.parse_fragment(frag / "features" / "on.h") == [
        ("define", "LEVEL", "3"),
        ("define", "FEAT", None),
    ]
    assert cf.parse_fragment(frag / "platform" / "host.h") == [("undef", "LEVEL", None)]
    assert cf.parse_fragment(frag / "toolchain" / "cc.h") == []
    loop = _write(tmp_path / "loop" / "a.h", '#include "b.h"\n')
    _write(tmp_path / "loop" / "b.h", '#include "a.h"\n')
    with pytest.raises(MatrixError, match="includes itself"):
        cf.parse_fragment(loop)
    bad = _write(tmp_path / "bad.h", "#define OK\nint x;\n")
    with pytest.raises(MatrixError, match=r"bad\.h:2: a fragment line"):
        cf.parse_fragment(bad)
    rows = cf.load_matrix(repo / "matrix.toml", repo).rows
    assert cf.missing_fragments(rows, frag) == []
    (frag / "target" / "t.h").unlink()
    assert cf.missing_fragments(rows, frag) == ["target/t.h"]
    assert cf.fragment_path(frag, "target", "t") == frag / "target" / "t.h"


def test_ALX1564_P360_a_row_header_is_the_base_with_its_fragments_applied(repo):
    rows = cf.load_matrix(repo / "matrix.toml", repo).rows
    on = cf.row_config(repo / "base.h", repo / "frag", rows[0])
    assert "\n#define FEAT\n" in on
    assert "\n#define LEVEL 3 // the level\n" in on  # the value replaced, the comment kept
    assert '\n#define EXAMPLE "one"\n//#define EXAMPLE "two"\n' in on  # the first line only
    host = cf.row_config(repo / "base.h", repo / "frag", rows[2])
    assert '//#define EXAMPLE "one"\n//#define EXAMPLE "two"\n' in host
    assert "\n//#define FEAT\n" in host
    # a value given to a line without a comment, an undef of a name never declared
    assert cf.synthesize("//#define A 1\n", [("define", "A", "2"), ("undef", "Z", None)]) == (
        "#define A 2\n"
    )
    with pytest.raises(MatrixError, match="Z: not declared"):
        cf.synthesize(BASE, [("define", "Z", None)])
    appended = cf.synthesize(
        BASE,
        [("define", "Z", "7"), ("define", "W", None), ("define", "V", None), ("undef", "V", None)],
        append=True,
    )
    assert appended.endswith(
        "// Matrix: switches the base header does not declare\n#define Z 7\n#define W\n#endif\n"
    )
    assert cf.synthesize("int x;", [("define", "Q", None)], append=True) == (
        "int x;\n// Matrix: switches the base header does not declare\n#define Q\n"
    )


# -- compilers and their output ---------------------------------------------------------------------
def test_ALX1564_P361_the_argv_of_a_source_carries_mode_flags_defines_and_folders_in_order(
    tmp_path,
):
    cc = cf.Compiler(
        "cc", Path("cc"), target_flags=("--target=arm",), host_flags=("--host",),
        diagnostics=("-plain",), warnings=("-Wall",), std="c11",
    )  # fmt: skip
    target = cf.Platform("p", includes=(tmp_path / "shim",), defines=("PART",), flags=("-mthumb",))
    host = cf.Platform("h", host=True)
    src = tmp_path / "a.c"
    argv = cf.compile_argv(cc, target, src, [tmp_path / "cfg", tmp_path / "lib"], "syntax", ["S=1"])
    assert argv == [
        "cc", "-std=c11", "--target=arm", "-mthumb", "-plain", "-fsyntax-only", "-Wall",
        "-DS=1", "-DPART", f"-I{tmp_path / 'cfg'}", f"-I{tmp_path / 'lib'}",
        f"-I{tmp_path / 'shim'}", str(src),
    ]  # fmt: skip
    assert cf.compile_argv(cc, host, src, [], "coverage")[:4] == [
        "cc",
        "-std=c11",
        "--host",
        "-plain",
    ]
    assert cf.compile_argv(cc, host, src, [], "coverage")[4:] == ["-E", str(src)]
    assert cf.compile_argv(cc, host, src, [], "deps")[4:] == ["-E", "-M", "-MG", str(src)]
    done = cf.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"])
    assert (done.returncode, done.stdout.strip(), done.stderr.strip()) == (0, "out", "err")


def test_ALX1564_P362_compiler_output_becomes_findings_headers_and_identifiers(tmp_path):
    output = "\n".join(
        [
            "X:\\repo\\a.c:3:10: fatal error: 'zephyr/kernel.h' file not found",
            "a.c:4:10: fatal error: lwip/api.h: No such file or directory",
            "a.c: In function 'f':",
            "a.c:5:2: error: 'GPIOA' undeclared (first use in this function)",
            "a.c:5:2: note: each undeclared identifier is reported only once",
            "a.c:6:2: error: implicit declaration of function 'HAL_Init' [-Wimplicit-function-declaration]",
            "a.c:7:2: error: call to undeclared function 'k_sleep'; ISO C99 and later do not support",
            "a.c:8:2: error: use of undeclared identifier 'NVIC'",
            "a.c:9:1: error: unknown type name 'TaskHandle_t'",
            "a.c:10:6: error: 'ADC_HandleTypeDef' has no member named 'Init'",
            "a.c:11:6: error: no member named 'Mode' in 'struct x'",
            "a.c:12:6: warning: unused variable 'y' [-Wunused-variable]",
            "a.c:13:6: error: use of undeclared identifier 'NVIC'",
        ]
    )
    findings = cf.parse_diagnostics(output)
    assert len(findings) == 11
    assert findings[0] == cf.Finding(
        "X:\\repo\\a.c", 3, 10, "fatal error", "'zephyr/kernel.h' file not found"
    )
    assert findings[-2].render() == "a.c:12:6: warning: unused variable 'y' [-Wunused-variable]"
    assert [cf.missing_header(f) for f in findings[:3]] == ["zephyr/kernel.h", "lwip/api.h", None]
    assert cf.undeclared(findings) == [
        "GPIOA", "HAL_Init", "k_sleep", "NVIC", "TaskHandle_t", "ADC_HandleTypeDef.Init", "x.Mode",
    ]  # fmt: skip
    found = _write(tmp_path / "found.h", "")
    rule = (
        f"a.o: a.c {found.as_posix()} \\\n  zephyr/kernel.h my\\ dir/x.h \\\r\n zephyr/kernel.h\n"
    )
    assert cf.unresolved_headers(rule) == ["zephyr/kernel.h", "my dir/x.h"]


# -- coverage --------------------------------------------------------------------------------------
SHAPE = """#include "x.h" /* a comment
spanning lines */
int top;
#if defined(A)
int a; // a comment
#  ifdef B
int b;
#  elif C
int c;
#  else
int d;
#  endif
#elif defined(E) \\
      || defined(F)
int e = \\
    1;
#endif
#else
#endif
const char* s = "// not a comment";
char q = '"';
/* unterminated
"""


def test_ALX1564_P363_the_structure_of_a_source_is_its_branches_and_code_lines():
    shape = cf.structure(SHAPE)
    assert shape.code == frozenset({3, 5, 7, 9, 11, 15, 20, 21})
    assert shape.guard == (
        "#if defined(A)",
        "#ifdef B",
        "#elif C (after #ifdef B)",
        "#else (after #ifdef B)",
        "#elif defined(E) \\ (after #if defined(A))",
    )
    assert shape.parent == (-1, 0, 0, 0, -1)
    assert [shape.owner[n] for n in (3, 5, 7, 9, 11, 15, 16, 18, 20)] == [
        -1,
        0,
        1,
        2,
        3,
        4,
        4,
        -1,
        -1,
    ]
    assert (
        cf.strip_comments('a // b\nc /* d\ne */ f "g // h" \'"\'')
        == 'a \nc     \n     f "g // h" \'"\''
    )
    assert cf.strip_comments('"unterminated\nx') == '"unterminated\nx'
    assert cf.strip_comments("x // end") == "x "


def test_ALX1564_P364_coverage_is_by_branch_and_every_gap_is_named_with_its_guard():
    shape = cf.structure(SHAPE)
    pre = '\n'.join(['junk before any marker', '# 1 "X:\\\\repo\\\\x.c" 1', "", "", "int top;",
                     "", "", "", "int b;"])  # fmt: skip
    emitted = cf.emitted_lines(pre)
    assert emitted == {"X:\\repo\\x.c": {3, 7}}
    taken = cf.taken_branches(shape, [*emitted["X:\\repo\\x.c"], 0, 999])
    assert taken == {0, 1}  # B's branch, and A's with it
    assert cf.gaps(shape, taken, reached=True) == [
        (9, 9, "#elif C (after #ifdef B)"),
        (11, 11, "#else (after #ifdef B)"),
        (15, 15, "#elif defined(E) \\ (after #if defined(A))"),
    ]
    assert cf.gaps(shape, set(), reached=True)[0] == (5, 5, "#if defined(A)")
    two = cf.structure("#if X\nint a;\nint b;\n\n// c\nint d;\n#endif\n")
    assert cf.gaps(two, set(), reached=True) == [(2, 6, "#if X")]
    split = cf.structure("#if X\nint a;\n#endif\nint b;\n#if X\nint c;\n#endif\n")
    assert cf.gaps(split, set(), reached=True) == [(2, 2, "#if X"), (6, 6, "#if X")]
    assert cf.gaps(shape, set(), reached=False) == [(3, 21, cf.UNREACHED)]
    assert cf.gaps(cf.structure("// nothing\n"), set(), reached=False) == []
    exemption = cf.Exemption("lib/*.h", "B", "why")
    assert exemption.covers("lib/x.h", "#ifdef B")
    assert not exemption.covers("lib/x.h", "#ifdef BB")
    assert not exemption.covers("src/x.h", "#ifdef B")
    assert cf.Exemption("lib/x.h", "", "why").covers("lib/x.h", cf.UNREACHED)
    assert cf.Gap(1, 2, "#if X").as_json() == [1, 2, "#if X"]
    assert cf.Gap(1, 2, "#if X", "why").as_json() == [1, 2, "#if X", "why"]


# -- the run -----------------------------------------------------------------------------------------
def test_ALX1564_P365_a_run_writes_every_row_its_verdict_and_the_coverage(repo, tmp_path):
    scripted = ScriptedCompiler(repo)
    out = tmp_path / "out"
    logged: list[str] = []
    report = cf.run_matrix(
        _matrix(repo, own=("own_",)), out, runner=scripted, workers=2, log=logged.append
    )
    assert [r["status"] for r in report["rows"]] == ["NO SHIM", "NO SHIM", "NO SHIM"]
    first = report["rows"][0]
    assert first["missing_headers"] == ["vendor.h", "deep/also missing.h"]
    assert first["findings_total"] == 1
    assert report["rows"][2]["findings_total"] == 2  # the elided row's warning in a.c
    assert logged[0].startswith("NO SHIM 0001_cc_p_on_t_all (1 finding(s))")
    config = out / "0001_cc_p_on_t_all" / "config"
    assert "\n#define FEAT\n" in (config / "cfg.h").read_text(encoding="ascii")
    assert (config / "cfg_usr.h").read_text(encoding="ascii") == (
        f'#include "{(repo / "lib" / "lib.h").as_posix()}"\n'
    )
    assert (out / "0003_cc_host_off_t_elided" / "findings.txt").read_text(encoding="utf-8").count(
        "\n"
    ) == 2
    # the dependency pass runs only for the source that lacks a header, coverage only on measured rows
    deps = [c for c in scripted.calls if "-M" in c]
    assert {Path(c[-1]).name for c in deps} == {"c.c"}
    assert len(deps) == 3
    coverage_calls = [c for c in scripted.calls if "-E" in c and "-M" not in c]
    assert len(coverage_calls) == 4  # two measured rows, two sources
    assert report["coverage"] == {
        "files": 4, "code_lines": 10, "covered": 6, "exempt": 3, "uncovered": 1,
    }  # fmt: skip
    assert report["uncovered"] == {"lib/c.c": [[2, 2, cf.UNREACHED]]}
    assert report["exempted"] == {
        "lib/lib.h": [
            [4, 4, "#ifdef __cplusplus", "C++ linkage"],
            [8, 8, "#ifdef __cplusplus", "C++ linkage"],
        ],
        "lib/unused.h": [[2, 2, cf.UNREACHED, "a template nobody includes"]],
    }
    assert report["source"]["compilers"] == {"cc": "cc 9.9.9"}
    assert report["source"]["arguments"] == {"only": [], "rows": 3, "of": 3}
    assert json.loads((out / "report.json").read_text(encoding="utf-8")) == report
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "3 of 3 rows, 2 sources each. Code lines: 10, covered 6, exempt 3, uncovered 1." in md
    assert "Missing headers: `vendor.h`, `deep/also missing.h`" in md
    assert "- `lib/unused.h`\n  - 2: `no source of any row includes this file` - a template" in md

    # without the missing header and with the repository's own name undeclared: FAIL, not NO SHIM
    plain = _matrix(repo, sources=[repo / "lib" / "a.c"], own=("own_",))

    def own_name(argv, env=None):
        done = scripted(argv, env)
        if "-fsyntax-only" in argv and "0001" in " ".join(argv):
            err = f"{argv[-1]}:5:2: error: use of undeclared identifier 'own_thing'\n"
            return subprocess.CompletedProcess(argv, 1, "", err)
        return done

    again = cf.run_matrix(plain, tmp_path / "again", runner=own_name, only=["0001", "0002"])
    assert [r["status"] for r in again["rows"]] == ["FAIL", "PASS"]
    assert again["rows"][0]["undeclared"] == ["own_thing"]
    assert again["uncovered"] == {"lib/c.c": [[2, 2, cf.UNREACHED]]}
    md = (tmp_path / "again" / "report.md").read_text(encoding="utf-8")
    assert "Undeclared: `own_thing`" in md
    assert "## 0002" not in md  # a passing row has no section

    # a compiler that fails without saying why is a finding all the same; without --version output
    # the report names the executable; findings beyond the kept number are counted, not quoted
    def silent(argv, env=None):
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "-fsyntax-only" in argv:
            many = "".join(f"{argv[-1]}:{n}:1: warning: w{n}\n" for n in range(1, 210))
            return subprocess.CompletedProcess(
                argv,
                0 if "0002" in " ".join(argv) else 3,
                many if "0002" in " ".join(argv) else "",
                "segfault",
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    quiet = cf.run_matrix(plain, tmp_path / "quiet", runner=silent, only=["0001", "0002"])
    assert quiet["rows"][0]["findings"] == [
        f"{repo / 'lib' / 'a.c'}:0:0: error: exit 3 without a diagnostic: segfault"
    ]
    assert quiet["rows"][1]["findings_total"] == 209
    assert len(quiet["rows"][1]["findings"]) == cf.FINDINGS_KEPT
    assert quiet["source"]["compilers"] == {"cc": "cc.exe"}
    assert quiet["coverage"]["covered"] == 0
    md = (tmp_path / "quiet" / "report.md").read_text(encoding="utf-8")
    assert "\n...\n```" in md
    assert "## Lines explained by an exemption\n\n- `lib/unused.h`" in md
    assert "- `lib/lib.h`\n  - 4-8: `no source of any row includes this file`\n" in md

    clean = _matrix(repo, sources=[repo / "lib" / "a.c"], measured=[repo / "lib" / "a.c"])
    tidy = cf.run_matrix(clean, tmp_path / "tidy", runner=scripted, only=["0001", "0002"])
    md = (tmp_path / "tidy" / "report.md").read_text(encoding="utf-8")
    assert tidy["uncovered"] == {}
    assert "## Lines no row compiled\n\nnone\n\n## Lines explained by an exemption\n\nnone\n" in md


def test_ALX1564_P366_a_run_refuses_words_it_cannot_mean_and_selections_that_match_nothing(
    repo, tmp_path
):
    with pytest.raises(MatrixError, match="no row matches"):
        cf.run_matrix(_matrix(repo), tmp_path, only=["9999"], runner=ScriptedCompiler(repo))
    (repo / "frag" / "library" / "elided.h").unlink()
    with pytest.raises(MatrixError, match=r"fragment library/elided.h missing"):
        cf.run_matrix(_matrix(repo), tmp_path, runner=ScriptedCompiler(repo))
    nothing = _matrix(repo, compilers={}, platforms={"q": cf.Platform("q")})
    with pytest.raises(MatrixError, match="no compiler for 'cc'"):
        nothing.check(nothing.matrix.rows[:1])
    bare = _matrix(repo, matrix=cf.parse_matrix(
        '[[row]]\nid = "0001"\ntoolchain = "cc"\nplatform = "q"\nfeatures = "on"\ntarget = "t"\n'
        'library = "all"\ndescription = "q"', repo))  # fmt: skip
    _write(repo / "frag" / "platform" / "q.h", "")
    with pytest.raises(MatrixError, match="no platform 'q'"):
        bare.check(bare.matrix.rows)
    defined = _matrix(repo, matrix=bare.matrix, platforms={"q": cf.Platform("q", flags=("-q",))})
    defined.check(defined.matrix.rows)
    assert defined.platform("q").flags == ("-q",)
    assert _matrix(repo).platform("p").defines == ("PART",)


# -- members and further headers --------------------------------------------------------------------
def test_ALX1564_P369_a_missing_member_is_named_with_its_structure_and_judged_by_it(repo, tmp_path):
    output = "\n".join(
        [
            "a.c:1:6: error: 'AlxNet' has no member named 'cellular'",
            "a.c:2:6: error: 'SPI_TypeDef' {aka 'struct shim_Struct'} has no member named 'CR9'",
            "a.c:3:6: error: 'union u' has no member named 'w'",
            "a.c:4:6: error: no member named 'k' in 'struct own_s'",
            "a.c:5:6: error: 'struct <anonymous>' has no member named 'anon'",
        ]
    )
    assert cf.undeclared(cf.parse_diagnostics(output)) == [
        "AlxNet.cellular", "SPI_TypeDef.CR9", "u.w", "own_s.k", "anon",
    ]  # fmt: skip
    scripted = ScriptedCompiler(repo)
    plain = _matrix(repo, sources=[repo / "lib" / "a.c"], own=("own_",))

    def member(argv, env=None):
        if "-fsyntax-only" in argv:
            owner = "own_s" if "0001" in " ".join(argv) else "vendor_s"
            err = f"{argv[-1]}:5:2: error: no member named 'k' in 'struct {owner}'\n"
            return subprocess.CompletedProcess(argv, 1, "", err)
        return scripted(argv, env)

    report = cf.run_matrix(plain, tmp_path / "member", runner=member, only=["0001", "0002"])
    assert [r["status"] for r in report["rows"]] == ["FAIL", "NO SHIM"]
    assert [r["undeclared"] for r in report["rows"]] == [["own_s.k"], ["vendor_s.k"]]


def test_ALX1564_P370_further_headers_take_the_names_they_declare_and_the_base_the_rest(
    repo, tmp_path
):
    _write(
        repo / "prod.h",
        "#ifndef PROD_H\n#define PROD_H\n#define PROD_APP\n//#define PROD_TEST\n#define LEVEL 9\n"
        "#endif\n",
    )
    _write(repo / "frag" / "target" / "t.h", "#define PROD_TEST\n#undef PROD_APP\n")
    product = _matrix(repo, headers={"prod.h": repo / "prod.h"})
    row = product.matrix.rows[0]
    headers = cf.row_headers(product, row)
    assert list(headers) == ["cfg.h", "prod.h"]
    assert headers["prod.h"] == (
        "#ifndef PROD_H\n#define PROD_H\n//#define PROD_APP\n#define PROD_TEST\n#define LEVEL 3\n"
        "#endif\n"
    )  # LEVEL is declared by both headers, so both take it
    assert headers["cfg.h"] == cf.synthesize(BASE, [("define", "FEAT", None), ("define", "LEVEL", "3"),
                                                    ("define", "EXAMPLE", None)])  # fmt: skip
    _write(repo / "frag" / "target" / "t.h", "#define PROD_TEST\n#define BUILD 1\n")
    with pytest.raises(MatrixError, match="BUILD: not declared"):
        cf.row_headers(product, row)
    folder = cf.write_config(_matrix(repo, headers={"prod.h": repo / "prod.h"}, append=True), row,
                             tmp_path / "w")  # fmt: skip
    assert sorted(p.name for p in folder.iterdir()) == ["cfg.h", "cfg_usr.h", "prod.h"]
    assert (
        (folder / "cfg.h")
        .read_text(encoding="utf-8")
        .endswith("// Matrix: switches the base header does not declare\n#define BUILD 1\n#endif\n")
    )
    assert "#define PROD_TEST\n" in (folder / "prod.h").read_text(encoding="utf-8")
    assert "PROD_TEST" not in (folder / "cfg.h").read_text(encoding="utf-8")
