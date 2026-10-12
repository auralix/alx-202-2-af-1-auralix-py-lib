# SPDX-License-Identifier: MIT
"""alx.verify.analyzers: clang-tidy and cppcheck over the rows of a configuration matrix (D33).

No analyser runs here, as no compiler runs in the configs tests: the run goes through a scripted
``runner`` that answers as clang-tidy, cppcheck and a preprocessor would, from the row's own
configuration header. The real analysers run in the C repositories' analyze lanes.

Proofs (ALX-1564):
  P689 an analyser's output becomes findings: clang-tidy's and cppcheck's one-line form, a
       Windows path, the check named in the text; notes and summaries are not findings
  P690 the flags an analyser takes for a row are the compiler's without the executable, a mode
       or the warnings: target or host flags, platform flags, defines, include folders in order
  P691 every named row runs as configured and with the disabled operations after its own: each
       source through clang-tidy with the tidy compiler's view of the row (a header read as C),
       the row through cppcheck once per option set with its toolchain's or the host's macros
       and only the row's defines and include folders, the preprocessor only as configured
  P692 a finding counts in a measured file only, once however many sources reach it; an accept
       entry keeps it with its reason; each row and variant writes its findings.txt and logs
  P693 the rows' preprocessed lines are the coverage, the gaps named; report.json and report.md
       hold the rows, the variants, the analysers' versions; an analyser not given does not run;
       a selection that matches no named row is refused
  P698 with clang-tidy the run writes the rows' compile database as configured: each source of
       each row once, the tidy compiler's view, a header read as C
  P700 clang-tidy's own flags come before the row's, after a header's -xc; the database does not
       carry them
"""

import json
import subprocess
from pathlib import Path

import pytest

from alx.verify import analyzers as an
from alx.verify import configs as cf

BASE = """#ifndef CFG_H
#define CFG_H
//#define FEAT
#define CHECKS
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

LIB_H = """#ifndef LIB_H
#define LIB_H
int a(void);
#endif
"""

MATRIX = """
defines = ["SHARED=1"]
includes = ["shim"]

[platform.p]
description = "the platform"
includes = ["shim/p"]
defines = ["PART"]
flags = ["-mthumb"]

[platform.host]
description = "the PC"
host = true

[[accept]]
file = "lib/*.c"
text = "[kept-id]"
why = "kept by decision"

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
platform = "host"
features = "off"
target = "t"
library = "all"
description = "feature off, on the PC"
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii", newline="\n")
    return path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _write(root / "base.h", BASE)
    _write(root / "lib" / "a.c", LIB_A)
    _write(root / "lib" / "lib.h", LIB_H)
    _write(root / "shim" / "x.h", "int x;\n")
    frag = root / "frag"
    for rel, text in {
        "toolchain/cc.h": "",
        "platform/p.h": "",
        "platform/host.h": "",
        "features/on.h": "#define FEAT\n",
        "features/off.h": "#undef FEAT\n",
        "target/t.h": "",
        "library/all.h": "",
    }.items():
        _write(frag / rel, text)
    _write(root / "matrix.toml", MATRIX)
    return root


def _matrix(root: Path) -> cf.Matrix:
    compilers = {
        "cc": cf.Compiler("cc", Path("cc.exe"), target_flags=("--target=arm",)),
        "clang": cf.Compiler(
            "clang",
            Path("clang.exe"),
            target_flags=("--target=arm-none-eabi", "-isystemNEWLIB"),
            host_flags=("--target=i686", "-DCRT"),
            env={"CLANG": "1"},
        ),
    }
    return cf.Matrix(
        root=root,
        base=root / "base.h",
        fragments=root / "frag",
        matrix=cf.load_matrix(root / "matrix.toml", root),
        sources=[root / "lib" / "a.c", root / "lib" / "lib.h"],
        measured=[root / "lib" / "a.c", root / "lib" / "lib.h"],
        includes=[root / "lib"],
        compilers=compilers,
        config_name="cfg.h",
        heads={"repo": "abc1234"},
    )


TIDY = an.Tidy(Path("tidy.exe"), Path("repo.clang-tidy"))
CPPCHECK = an.Cppcheck(
    Path("cppcheck.exe"),
    options=("--std=c99",),
    runs=(("--platform=unix32",), ("--platform=win64",)),
    defines={"cc": ("__GNUC__",), "host": ("_WIN32",)},
)
DISABLED = [("undef", "CHECKS", None)]


def _config(argv) -> str:
    folder = next(a[2:] for a in argv if a.startswith("-I"))
    return (Path(folder) / "cfg.h").read_text(encoding="ascii")


class Scripted:
    """Answers as clang-tidy, cppcheck and a preprocessor would for the repository's two files."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[list[str], object]] = []

    def __call__(self, argv, env=None):
        argv = list(argv)
        self.calls.append((argv, env))
        a_c, lib_h = self.root / "lib" / "a.c", self.root / "lib" / "lib.h"
        tool = Path(argv[0]).stem
        if argv[-1] == "--version":
            text = {
                "tidy": "LLVM (http://llvm.org/):\n  LLVM version 22.1.8\n",
                "cppcheck": "Cppcheck 2.21\n",
            }
            return subprocess.CompletedProcess(argv, 0, text[tool], "")
        config = _config(argv)
        disabled = "//#define CHECKS" in config
        if "-E" in argv:
            source = Path(argv[-1])
            if source.name == "lib.h":
                return subprocess.CompletedProcess(
                    argv, 0, f'# 1 "{source.as_posix()}"\n\n\nint a(void);\n', ""
                )
            body = (
                "\nint a(void)\n{\n\n\treturn 1;\n\n\n\n}\n"
                if "\n#define FEAT" in config
                else ("\nint a(void)\n{\n\n\n\n\treturn 0;\n\n}\n")
            )
            outside = '# 1 "X:\\\\outside\\\\x.h" 1\nint x;\n'  # a header outside the repository
            return subprocess.CompletedProcess(
                argv, 0, f'# 1 "{source.as_posix()}"\n{body}{outside}', ""
            )
        if tool == "tidy":
            source = Path(argv[3])
            out = (
                f"{lib_h}:3:1: warning: in the header [readability-x]\n"  # every source reaches it
            )
            if source.name == "a.c":
                out += f"{a_c}:5:2: warning: dead store [clang-analyzer-deadcode.DeadStores]\n"
                out += f"{a_c}:5:2: note: the value stored here\n"
                out += f"{self.root / 'shim' / 'x.h'}:1:1: warning: in a fake [bugprone-x]\n"
                if disabled:
                    out += f"{a_c}:7:2: warning: never read [clang-analyzer-deadcode.DeadStores]\n"
            out += "Suppressed 3 warnings (3 in non-user code).\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        out = (
            f"{a_c}:2:1: style: kept on purpose [kept-id]\n{a_c}:9:1: portability: once [port-id]\n"
        )
        return subprocess.CompletedProcess(argv, 1, "", out)


def test_ALX1564_P689_an_analysers_output_becomes_findings():
    output = (
        "D:\\r\\lib\\a.c:3:5: warning: Value stored to 'x' is never read [clang-analyzer-deadcode.DeadStores]\n"
        "D:\\r\\lib\\a.c:3:5: note: Value stored to 'x' is never read\n"
        "  lib/b.c:10:1: style: The scope of the variable 'y' can be reduced. [variableScope]\n"
        "lib/c.c:1:1: error: unknown type name 'u8' [clang-diagnostic-error]\n"
        "Suppressed 3 warnings (3 in non-user code).\n"
    )
    found = an.parse_findings("clang-tidy", output)
    assert found == [
        an.Finding("clang-tidy", "D:\\r\\lib\\a.c", 3, "warning",
                   "Value stored to 'x' is never read [clang-analyzer-deadcode.DeadStores]"),
        an.Finding("clang-tidy", "lib/b.c", 10, "style",
                   "The scope of the variable 'y' can be reduced. [variableScope]"),
        an.Finding("clang-tidy", "lib/c.c", 1, "error", "unknown type name 'u8' [clang-diagnostic-error]"),
    ]  # fmt: skip
    assert found[1].render() == (
        "lib/b.c:10: clang-tidy style: The scope of the variable 'y' can be reduced. [variableScope]"
    )


def test_ALX1564_P690_the_flags_an_analyser_takes_are_the_compilers_without_mode_or_warnings():
    compiler = cf.Compiler(
        "cc", Path("cc.exe"), target_flags=("--target=arm",), host_flags=("--target=pc",),
        diagnostics=("-fno-color",), warnings=("-Wall", "-Werror"),
    )  # fmt: skip
    platform = cf.Platform("p", "", False, (Path("shim/p"),), ("PART",), ("-mthumb",))
    flags = an.compile_flags(compiler, platform, [Path("cfg"), Path("lib")], ["SHARED=1"])
    assert flags == [
        "-std=gnu99", "--target=arm", "-mthumb", "-fno-color", "-DSHARED=1", "-DPART",
        f"-I{Path('cfg')}", f"-I{Path('lib')}", f"-I{Path('shim/p')}",
    ]  # fmt: skip
    host = cf.Platform("host", "", True, (), (), ())
    assert an.compile_flags(compiler, host, [])[:2] == ["-std=gnu99", "--target=pc"]


def test_ALX1564_P691_every_row_runs_as_configured_and_disabled_through_both_analysers(
    repo, tmp_path
):
    scripted = Scripted(repo)
    out = tmp_path / "out"
    an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, out, tidy=TIDY, cppcheck=CPPCHECK,
                    disabled=DISABLED, runner=scripted, workers=2)  # fmt: skip
    calls = [(argv, env) for argv, env in scripted.calls if argv[-1] != "--version"]
    tidy = [(argv, env) for argv, env in calls if argv[0] == "tidy.exe"]
    cpp = [argv for argv, _ in calls if argv[0] == "cppcheck.exe"]
    pre = [argv for argv, _ in calls if "-E" in argv]
    assert len(tidy) == 2 * 2 * 2  # rows x variants x sources
    assert len(cpp) == 2 * 2 * 2  # rows x variants x option sets
    assert len(pre) == 2 * 2  # rows x sources, as configured only
    a_c, lib_h = str(repo / "lib" / "a.c"), str(repo / "lib" / "lib.h")
    first, env = tidy[0]
    folder = out / "0001_cc_p_on_t_all" / "configured" / "config"
    assert first == [
        "tidy.exe", "--quiet", "--config-file=repo.clang-tidy", a_c, "--",
        "-std=gnu99", "--target=arm-none-eabi", "-isystemNEWLIB", "-mthumb", "-DSHARED=1", "-DPART",
        f"-I{folder}", f"-I{repo / 'lib'}", f"-I{repo / 'shim'}", f"-I{repo / 'shim' / 'p'}",
    ]  # fmt: skip
    assert env == {"CLANG": "1"}  # the tidy compiler's environment
    header = next(argv for argv, _ in tidy if argv[3] == lib_h)
    assert header[5] == "-xc"
    host = next(argv for argv, _ in tidy if "0002_cc_host_off_t_all" in " ".join(argv))
    assert host[6:8] == ["--target=i686", "-DCRT"]
    assert cpp[0] == [
        "cppcheck.exe", "--std=c99", "--platform=unix32", an.CPPCHECK_TEMPLATE, "-D__GNUC__",
        "-DSHARED=1", "-DPART", f"-I{folder}", f"-I{repo / 'lib'}", f"-I{repo / 'shim'}",
        f"-I{repo / 'shim' / 'p'}", a_c, lib_h,
    ]  # fmt: skip
    assert cpp[1][2] == "--platform=win64"
    assert [argv[4] for argv in cpp if "0002" in " ".join(argv)] == ["-D_WIN32"] * 4
    assert pre[0][:3] == ["cc.exe", "-std=gnu99", "--target=arm"]  # the row's own compiler
    assert all("configured" in " ".join(argv) for argv in pre)
    disabled = (out / "0001_cc_p_on_t_all" / "disabled" / "config" / "cfg.h").read_text(
        encoding="ascii"
    )
    assert "//#define CHECKS" in disabled
    assert "\n#define FEAT" in disabled  # the row's own operations first, the disabled ones after
    assert "\n#define CHECKS" in (folder / "cfg.h").read_text(encoding="ascii")


def test_ALX1564_P692_a_finding_counts_once_in_a_measured_file_unless_kept(repo, tmp_path):
    out = tmp_path / "out"
    logged: list[str] = []
    report = an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, out, tidy=TIDY, cppcheck=CPPCHECK,
                             disabled=DISABLED, runner=Scripted(repo), log=logged.append)  # fmt: skip
    a_c, lib_h = repo / "lib" / "a.c", repo / "lib" / "lib.h"
    runs = {(r["row"], r["variant"]): r for r in report["runs"]}
    configured = runs[("0001_cc_p_on_t_all", "configured")]
    assert configured["findings"] == [
        f"{lib_h}:3: clang-tidy warning: in the header [readability-x]",
        f"{a_c}:5: clang-tidy warning: dead store [clang-analyzer-deadcode.DeadStores]",
        f"{a_c}:9: cppcheck portability: once [port-id]",
    ]
    assert configured["accepted"] == [
        f"{a_c}:2: cppcheck style: kept on purpose [kept-id] - kept by decision"
    ]
    disabled = runs[("0001_cc_p_on_t_all", "disabled")]
    assert disabled["findings_total"] == 4
    assert (
        f"{a_c}:7: clang-tidy warning: never read [clang-analyzer-deadcode.DeadStores]"
        in disabled["findings"]
    )
    written = (out / "0001_cc_p_on_t_all" / "disabled" / "findings.txt").read_text(encoding="utf-8")
    assert written.count("\n") == 4
    assert logged == [
        "    3 finding(s)  0001_cc_p_on_t_all configured",
        "    4 finding(s)  0001_cc_p_on_t_all disabled",
        "    3 finding(s)  0002_cc_host_off_t_all configured",
        "    4 finding(s)  0002_cc_host_off_t_all disabled",
    ]


def test_ALX1564_P693_coverage_reports_and_what_runs(repo, tmp_path):
    report = an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, tmp_path / "all", tidy=TIDY,
                             cppcheck=CPPCHECK, disabled=DISABLED, runner=Scripted(repo))  # fmt: skip
    assert report["coverage"] == {
        "files": 2,
        "code_lines": 6,
        "covered": 6,
        "exempt": 0,
        "uncovered": 0,
    }
    assert report["source"]["tools"] == {
        "clang-tidy": "LLVM version 22.1.8",
        "cppcheck": "Cppcheck 2.21",
    }
    assert report["source"]["arguments"] == {
        "only": [], "rows": ["0001_cc_p_on_t_all", "0002_cc_host_off_t_all"], "of": 2,
    }  # fmt: skip
    assert report["source"]["heads"] == {"repo": "abc1234"}
    assert json.loads((tmp_path / "all" / "report.json").read_text(encoding="utf-8")) == report
    md = (tmp_path / "all" / "report.md").read_text(encoding="utf-8")
    assert (
        "2 of 2 rows, each configured and disabled. Code lines: 6, compiled by these rows 6" in md
    )
    assert "| `0001_cc_p_on_t_all` | configured | 3 | 1 |" in md
    assert "## 0001_cc_p_on_t_all disabled\n\n```\n" in md
    assert "## Lines no analysed row compiled\n\nnone\n" in md
    assert md.endswith("[kept-id] - kept by decision\n")

    scripted = Scripted(repo)
    one = an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, tmp_path / "one", tidy=None,
                          cppcheck=None, runner=scripted, only=["0001"])  # fmt: skip
    assert {Path(argv[0]).stem for argv, _ in scripted.calls} == {"cc"}  # neither analyser ran
    assert one["source"]["tools"] == {}
    assert one["uncovered"] == {"lib/a.c": [[7, 7, "#else (after #if defined(FEAT))"]]}
    assert [(r["row"], r["variant"]) for r in one["runs"]] == [
        ("0001_cc_p_on_t_all", "configured"), ("0001_cc_p_on_t_all", "disabled"),
    ]  # fmt: skip
    md = (tmp_path / "one" / "report.md").read_text(encoding="utf-8")
    assert "- `lib/a.c`\n  - 7: `#else (after #if defined(FEAT))`" in md
    assert md.endswith("## Findings kept by decision\n\nnone\n")
    with pytest.raises(cf.MatrixError, match="no analysed row matches"):
        an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, tmp_path / "none", tidy=None,
                        cppcheck=None, runner=scripted, only=["9999"])  # fmt: skip


def test_ALX1564_P693_a_version_without_the_word_is_the_first_line_or_the_executable():
    def answer(text):
        return lambda argv, env=None: subprocess.CompletedProcess(argv, 0, text, "")

    assert an._version(Path("x.exe"), answer("Cppcheck 2.21\n")) == "Cppcheck 2.21"
    assert an._version(Path("x.exe"), answer("")) == "x.exe"


def test_ALX1564_P698_the_rows_compile_database_is_written_as_clang_tidy_read_them(repo, tmp_path):
    out = tmp_path / "out"
    an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, out, tidy=TIDY, cppcheck=None,
                    disabled=DISABLED, runner=Scripted(repo))  # fmt: skip
    database = json.loads((out / "compile_commands.json").read_text(encoding="utf-8"))
    a_c, lib_h = str(repo / "lib" / "a.c"), str(repo / "lib" / "lib.h")
    assert [(e["file"], Path(e["arguments"][-1]).name) for e in database] == [
        (a_c, "a.c"), (lib_h, "lib.h"), (a_c, "a.c"), (lib_h, "lib.h"),
    ]  # fmt: skip
    folder = out / "0001_cc_p_on_t_all" / "configured" / "config"
    assert database[0] == {
        "directory": str(out),
        "file": a_c,
        "arguments": [
            "clang.exe", "-std=gnu99", "--target=arm-none-eabi", "-isystemNEWLIB", "-mthumb",
            "-DSHARED=1", "-DPART", f"-I{folder}", f"-I{repo / 'lib'}", f"-I{repo / 'shim'}",
            f"-I{repo / 'shim' / 'p'}", "-c", a_c,
        ],
    }  # fmt: skip
    assert database[1]["arguments"][1] == "-xc"
    assert "--target=i686" in database[2]["arguments"]
    an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, tmp_path / "none", tidy=None,
                    cppcheck=None, runner=Scripted(repo))  # fmt: skip
    assert not (tmp_path / "none" / "compile_commands.json").exists()


def test_ALX1564_P700_clang_tidys_own_flags_come_before_the_rows(repo, tmp_path):
    scripted = Scripted(repo)
    tidy = an.Tidy(Path("tidy.exe"), Path("repo.clang-tidy"), flags=("-include", "analysis.h"))
    out = tmp_path / "out"
    an.run_analysis(_matrix(repo), _matrix(repo).matrix.rows, out, tidy=tidy, cppcheck=None,
                    runner=scripted, only=["0001"])  # fmt: skip
    runs = [argv for argv, _ in scripted.calls if argv[0] == "tidy.exe" and argv[-1] != "--version"]
    source = next(argv for argv in runs if argv[3].endswith("a.c"))
    assert source[4:8] == ["--", "-include", "analysis.h", "-std=gnu99"]
    header = next(argv for argv in runs if argv[3].endswith("lib.h"))
    assert header[4:8] == ["--", "-xc", "-include", "analysis.h"]
    database = json.loads((out / "compile_commands.json").read_text(encoding="utf-8"))
    assert all("analysis.h" not in entry["arguments"] for entry in database)
