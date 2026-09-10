# SPDX-License-Identifier: MIT
"""alx.c_lib.mutation_hooks: the three C hooks of the MUTATE lane, over a scripted compiler.

No compiler runs here. The argv is asserted directly and the exit code is scripted, the same way
the host_build tests work; the real clang is exercised by a C repository's own mutate lane.

Proofs (ALX-1544):
  P174 check asks the front end only, with the consumer's dialect, includes and defines
  P175 a mutant that does not compile is not valid C (exit 1), one that does is (exit 0)
  P176 the fingerprint compiles under a FIXED name, so two mutants differ only by their code
  P177 a COFF timestamp is zeroed before hashing and an ELF object is left alone
  P178 the fingerprint of a file that does not compile is None, not a crash and not a hash
  P179 load_groups imports the consumer's declaration by module:attribute and says so when it cannot
  P180 rebuild_groups builds only stale targets and reports the first failure with the target name
  P181 main(): the three subcommands, their exit codes, and the hash on stdout
  P183 the asking hooks do NOT capture the build environment: vcvars costs 1.1 s a call and a hook is one process
       per mutant, so paying it ~1400 times per source is twenty minutes of batch file for nothing
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from alx.c_lib import host_build
from alx.c_lib import mutation_hooks as mh

COFF = b"\x4c\x01\x02\x00" + b"\x11\x22\x33\x44" + b"body of the object"
ELF = mh.ELF_MAGIC + b"\x02\x01\x01\x00" + b"body of the object"


class FakeClang:
    """Records every argv; returns the scripted exit code and writes the scripted object."""

    def __init__(self, code=0, writes=None):
        self.calls: list[list[str]] = []
        self.envs: list[object] = []
        self.code = code
        self.writes = writes

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append(list(argv))
        self.envs.append(env)
        if self.writes is not None and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_bytes(self.writes)
        return subprocess.CompletedProcess(list(argv), self.code, stdout="", stderr="")

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def clang(monkeypatch, tmp_path):
    """A scripted compiler plus a Toolchain whose clang is a file that exists."""
    llvm = tmp_path / "llvm"
    llvm.mkdir()
    (llvm / ("clang.exe" if host_build.WINDOWS else "clang")).write_text("", encoding="ascii")
    monkeypatch.setenv("ALX_LLVM_DIR", str(llvm))
    fake = FakeClang()
    monkeypatch.setattr(host_build, "run", fake)
    toolchain = host_build.Toolchain()
    monkeypatch.setattr(toolchain, "environment", lambda: {"INCLUDE": "somewhere"})
    return fake, toolchain


def test_ALX1544_P174_check_asks_the_front_end_with_the_consumers_flags(clang, tmp_path):
    fake, toolchain = clang
    mh.syntax_ok(
        toolchain,
        tmp_path / "mutant.c",
        std="gnu11",
        includes=[tmp_path, "/other"],
        defines=["-DASSERTS_ON"],
    )
    argv = fake.last
    assert argv[0].endswith("clang.exe") or argv[0].endswith("clang")
    assert "-fsyntax-only" in argv, "the front end only: no code generated"
    assert "-std=gnu11" in argv, "the dialect is the consumer's, not a default of ours"
    assert "-w" in argv, "a mutant's warnings are not the question"
    assert host_build.CRT_DEFINE in argv
    assert "-DASSERTS_ON" in argv
    assert f"-I{tmp_path}" in argv
    assert "-I/other" in argv
    assert argv[-1] == str(tmp_path / "mutant.c")
    assert not any(a == "-o" for a in argv), "nothing is written"


def test_ALX1544_P175_a_mutant_that_does_not_compile_is_not_valid_c(clang, tmp_path):
    fake, toolchain = clang
    assert mh.syntax_ok(toolchain, tmp_path / "ok.c") is True
    fake.code = 1
    assert mh.syntax_ok(toolchain, tmp_path / "broken.c") is False


def test_ALX1544_P176_the_fingerprint_compiles_under_a_fixed_name(clang, tmp_path):
    fake, toolchain = clang
    fake.writes = COFF
    work = tmp_path / "work"
    first = tmp_path / "mutant_0007.c"
    first.write_bytes(b"int a = 1;\n")
    second = tmp_path / "mutant_0031.c"
    second.write_bytes(b"int a = 1;\n")

    digest_one = mh.object_fingerprint(toolchain, first, work)
    argv = fake.last
    assert str(work / f"{mh.WORK_STEM}.c") in argv, "the copy, not the mutant's own name"
    assert "mutant_0007" not in " ".join(argv), "the mutant's name never reaches the compiler"
    assert argv[argv.index("-o") + 1] == str(work / f"{mh.WORK_STEM}.o")
    assert "-O1" in argv

    digest_two = mh.object_fingerprint(toolchain, second, work)
    assert digest_one is not None
    assert digest_one == digest_two, "same code under two mutant names hashes alike"
    assert len(digest_one) == 64


def test_ALX1544_P177_a_coff_timestamp_is_zeroed_and_an_elf_object_is_left_alone():
    stamped = COFF
    other_time = COFF[:4] + b"\x99\x88\x77\x66" + COFF[8:]
    assert stamped != other_time, "the two differ only in the timestamp field"
    assert mh.strip_object_timestamp(stamped) == mh.strip_object_timestamp(other_time)
    assert mh.strip_object_timestamp(stamped)[4:8] == bytes(4)
    assert mh.strip_object_timestamp(stamped)[8:] == COFF[8:], "nothing else is touched"
    assert mh.strip_object_timestamp(ELF) == ELF, "an ELF object carries no timestamp"


def test_ALX1544_P178_the_fingerprint_of_a_file_that_does_not_compile_is_none(clang, tmp_path):
    fake, toolchain = clang
    fake.code = 1
    source = tmp_path / "broken.c"
    source.write_bytes(b"int a = nope;\n")
    assert mh.object_fingerprint(toolchain, source, tmp_path / "work") is None


def test_ALX1544_P179_load_groups_imports_the_consumers_declaration(tmp_path):
    module = tmp_path / "fake_conftest.py"
    module.write_text(
        "from pathlib import Path\nDLL_GROUPS = [(Path('a.dll'), [Path('a.c')], lambda: None)]\n",
        encoding="ascii",
    )
    groups = mh.load_groups("fake_conftest:DLL_GROUPS", [tmp_path])
    assert len(groups) == 1
    assert groups[0][0] == Path("a.dll")
    sys.path.remove(str(tmp_path))

    with pytest.raises(ValueError, match="module:attribute"):
        mh.load_groups("no_colon_here")
    with pytest.raises(ValueError, match="module:attribute"):
        mh.load_groups(":onlyattribute")


def test_ALX1544_P180_rebuild_builds_only_stale_targets_and_names_a_failure(tmp_path):
    built: list[str] = []
    current = tmp_path / "current.dll"
    dep = tmp_path / "dep.c"
    dep.write_text("int a;", encoding="ascii")
    current.write_text("dll", encoding="ascii")
    stamp = current.stat().st_mtime
    os.utime(dep, (stamp - 10, stamp - 10))
    missing = tmp_path / "missing.dll"

    groups = [
        (current, [dep], lambda: built.append("current")),
        (missing, [dep], lambda: built.append("missing")),
    ]
    assert mh.rebuild_groups(groups) is None
    assert built == ["missing"], "the current target is not rebuilt"

    def explode() -> None:
        raise host_build.BuildError("clang said no\n" + "x" * 3000)

    failure = mh.rebuild_groups([(missing, [dep], explode)])
    assert failure is not None
    assert failure.startswith("rebuild failed (missing.dll):")
    assert "clang said no" in failure
    assert len(failure) < 1600, "the compiler's wall of text is trimmed"


def test_ALX1544_P181_main_runs_the_three_hooks_and_reports(clang, tmp_path, capsys, monkeypatch):
    fake, toolchain = clang
    monkeypatch.setattr(host_build, "Toolchain", lambda: toolchain)
    source = tmp_path / "m.c"
    source.write_bytes(b"int a = 1;\n")

    assert mh.main(["check", str(source), f"-I{tmp_path}", "-DX"]) == 0
    assert "-fsyntax-only" in fake.last
    fake.code = 1
    assert mh.main(["check", str(source)]) == 1
    assert mh.main(["fingerprint", str(source), "--work", str(tmp_path / "w")]) == 1

    fake.code = 0
    fake.writes = COFF
    assert mh.main(["fingerprint", str(source), "--work", str(tmp_path / "w")]) == 0
    printed = capsys.readouterr().out.strip()
    assert len(printed) == 64

    module = tmp_path / "groups_ok.py"
    module.write_text("DLL_GROUPS = []\n", encoding="ascii")
    assert (
        mh.main(["rebuild", "--groups", "groups_ok:DLL_GROUPS", "--sys-path", str(tmp_path)]) == 0
    )

    module = tmp_path / "groups_bad.py"
    module.write_text(
        "from pathlib import Path\n"
        "def _boom():\n"
        "    raise RuntimeError('recipe exploded')\n"
        "DLL_GROUPS = [(Path('gone.dll'), [], _boom)]\n",
        encoding="ascii",
    )
    assert (
        mh.main(["rebuild", "--groups", "groups_bad:DLL_GROUPS", "--sys-path", str(tmp_path)]) == 1
    )
    assert "rebuild failed (gone.dll)" in capsys.readouterr().out


def test_ALX1544_P183_the_asking_hooks_do_not_capture_the_build_environment(clang, tmp_path):
    """Measured 10.09: 1.34 s per call with the vcvars environment, 0.22 s without it.

    A hook runs once per mutant in its own process, so the capture is paid again every time - it
    turned one C source's mutate run from 9 minutes into 29. These two hooks ask clang about a
    SOURCE, and clang finds the MSVC headers by itself; only the rebuild hook needs a build
    environment, and it gets one from the consumer's own recipe.
    """
    fake, toolchain = clang
    source = tmp_path / "m.c"
    source.write_bytes(b"int a = 1;\n")

    mh.syntax_ok(toolchain, source)
    assert fake.envs[-1] is None, "check must not hand the compiler a captured environment"

    fake.writes = COFF
    mh.object_fingerprint(toolchain, source, tmp_path / "w")
    assert fake.envs[-1] is None, "fingerprint must not either"
