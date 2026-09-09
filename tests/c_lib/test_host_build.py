# SPDX-License-Identifier: MIT
"""alx.c_lib.host_build: the host DLL build of a C repository, over a scripted compiler.

No compiler runs here. The argv builders are pure functions, so what a lane WILL run is asserted
directly; the steps that spawn something use a scripted ``subprocess.run`` that records the argv
and can be told to fail. The real toolchain is exercised on a bench, in the C repository's lanes.

Proofs (ALX-1544):
  P159 the two drivers translate the same build into their own flags (dialect, includes, output)
  P160 a GNU warning flag reaches the clang front-end under both drivers (clang-cl reads -Wall as
       /Wall = -Weverything, so it must be handed over as /clang:-Wall)
  P161 the closure step compiles with warnings off and never links
  P162 needs_build: missing target, newer dependency, vanished dependency, everything current
  P163 the compile database holds one entry per unique source with the build's own flags
  P164 build_dll one step: the link argv, and the DLL is not rebuilt when nothing changed
  P165 build_dll two steps: the closure objects are compiled first and linked into the DLL
  P166 a step that exits non-zero raises BuildError carrying the tool's own output
  P167 build_exe drops the shared-library flag; run_exe runs in the build environment
  P168 Toolchain: locations come from the environment variables, a missing one names its variable
  P169 the build environment is vcvars' variables over the process environment, captured once
"""

import json
import os
import subprocess
from typing import Any

import pytest

from alx.c_lib import host_build as hb

SRC = ["a.c", "b.c"]
INC = ["/inc/one", "/inc/two"]


class FakeCompiler:
    """Records every argv and returns the scripted exit code (0 unless a source name says fail)."""

    def __init__(self, fail_on=None, make=()):
        self.calls: list[dict[str, Any]] = []
        self.fail_on = fail_on
        self.make = list(make)  # file names to create in cwd on a compile-only call

    def __call__(self, argv, **kw):
        self.calls.append({"argv": list(argv), **kw})
        for name in self.make:
            if "/c" in argv or "-c" in argv:
                (kw["cwd"] / name).write_text("object", encoding="ascii")
        failed = self.fail_on is not None and any(self.fail_on in a for a in argv)
        return subprocess.CompletedProcess(
            list(argv), 1 if failed else 0, stdout="compiler said no", stderr="line 7"
        )


@pytest.fixture
def compiler(monkeypatch):
    fake = FakeCompiler()
    monkeypatch.setattr(hb, "run", lambda argv, cwd=None, env=None: fake(argv, cwd=cwd, env=env))
    return fake


@pytest.fixture
def toolchain(monkeypatch, tmp_path):
    """A Toolchain whose tools exist as empty files and whose environment needs no vcvars."""
    llvm = tmp_path / "llvm"
    llvm.mkdir()
    for name in ("clang", "clang-cl"):
        (llvm / (f"{name}.exe" if hb.WINDOWS else name)).write_text("", encoding="ascii")
    monkeypatch.setenv("ALX_LLVM_DIR", str(llvm))
    tc = hb.Toolchain()
    monkeypatch.setattr(tc, "environment", lambda: {"INCLUDE": "somewhere"})
    return tc


def test_ALX1544_P159_the_two_drivers_translate_the_same_build_into_their_own_flags(tmp_path):
    dll = tmp_path / "group.dll"
    gnu = hb.link_argv("clang", dll, SRC, driver=hb.GNU, includes=INC, def_file="g.def")
    assert gnu[0] == "clang"
    assert "-std=gnu99" in gnu
    assert [a for a in gnu if a.startswith("-I")] == ["-I/inc/one", "-I/inc/two"]
    assert gnu[-4:] == ["-shared", "-o", str(dll), "-Wl,/DEF:g.def"]
    assert "/LD" not in gnu

    msvc = hb.link_argv("clang-cl", dll, SRC, driver=hb.MSVC, includes=INC, def_file="m.def")
    assert msvc[1] == "/LD"
    assert "/clang:-std=gnu99" in msvc
    assert [a for a in msvc if a.startswith("/I")] == ["/I/inc/one", "/I/inc/two"]
    assert f"/Fe:{dll}" in msvc
    assert msvc[-2:] == ["/link", "/DEF:m.def"]

    # both carry the CRT define, the sources and the objects; neither invents a dialect
    for argv in (gnu, msvc):
        assert hb.CRT_DEFINE in argv
        assert "a.c" in argv
        assert "b.c" in argv
    # without an export list neither driver names one
    assert "/link" not in hb.link_argv("clang-cl", dll, SRC, driver=hb.MSVC)
    assert not any("DEF" in a for a in hb.link_argv("clang", dll, SRC, driver=hb.GNU))


def test_ALX1544_P160_a_gnu_warning_flag_reaches_the_front_end_under_both_drivers(tmp_path):
    strict = [*hb.WARNINGS, "-Werror"]
    gnu = hb.link_argv("clang", tmp_path / "x.dll", SRC, driver=hb.GNU, warnings=strict)
    msvc = hb.link_argv("clang-cl", tmp_path / "x.dll", SRC, driver=hb.MSVC, warnings=strict)
    assert "-Wall" in gnu
    assert "-Werror" in gnu
    # clang-cl reads a bare -Wall as MSVC's /Wall, which is -Weverything: hand it to the front-end
    assert "-Wall" not in msvc
    assert "/clang:-Wall" in msvc
    assert "/clang:-Werror" in msvc
    assert hb._warning_flags(hb.GNU, ["-Wshadow"]) == ["-Wshadow"]
    assert hb._warning_flags(hb.MSVC, ["-Wshadow"]) == ["/clang:-Wshadow"]


def test_ALX1544_P161_the_closure_step_compiles_with_warnings_off_and_never_links():
    gnu = hb.compile_argv("clang", SRC, driver=hb.GNU, includes=INC, defines=["-DON"])
    assert "-w" in gnu
    assert "-c" in gnu
    assert "-DON" in gnu
    assert not any(a.startswith(("-o", "-shared")) for a in gnu)

    msvc = hb.compile_argv("clang-cl", SRC, driver=hb.MSVC, flags=list(hb.UBSAN))
    assert "/w" in msvc
    assert "/c" in msvc
    assert "-fsanitize=undefined" in msvc
    assert "/LD" not in msvc
    assert hb.OBJECT_SUFFIX == {hb.GNU: ".o", hb.MSVC: ".obj"}


def test_ALX1544_P162_needs_build_sees_a_missing_target_a_newer_or_a_vanished_dependency(tmp_path):
    dep = tmp_path / "src.c"
    dep.write_text("int a;", encoding="ascii")
    target = tmp_path / "out.dll"
    assert hb.needs_build(target, [dep]) is True, "no target yet"

    target.write_text("dll", encoding="ascii")
    stamp = target.stat().st_mtime
    os.utime(dep, (stamp - 10, stamp - 10))
    assert hb.needs_build(target, [dep]) is False, "everything older than the target"

    os.utime(dep, (stamp + 10, stamp + 10))
    assert hb.needs_build(target, [dep]) is True, "a newer dependency"

    assert hb.needs_build(target, [tmp_path / "gone.h"]) is True, "a dependency that vanished"


def test_ALX1544_P163_the_compile_database_holds_one_entry_per_unique_source(tmp_path):
    a, b = tmp_path / "a.c", tmp_path / "b.c"
    args = ["clang", "-std=gnu99", "-I/inc"]
    entries = hb.compile_db([a, b, a], args, tmp_path)
    assert [e["file"] for e in entries] == [str(a), str(b)], "the repeat is dropped"
    assert entries[0]["arguments"] == [*args, "-c", str(a)]
    assert entries[0]["directory"] == str(tmp_path)

    out = tmp_path / "build" / "compile_commands.json"
    hb.write_compile_db(out, [a], args)
    written = json.loads(out.read_text(encoding="ascii"))
    assert written[0]["file"] == str(a)
    assert written[0]["directory"] == str(out.parent), "the default directory is the file's folder"


def test_ALX1544_P164_build_dll_one_step_links_the_gated_sources(tmp_path, toolchain, compiler):
    dll = tmp_path / "out" / "fifo.dll"
    result = hb.build_dll(
        toolchain,
        out=dll,
        strict=[tmp_path / "fifo.c"],
        includes=[tmp_path],
        def_file=tmp_path / "fifo.def",
        warnings=["-Werror"],
        driver=hb.MSVC,
    )
    assert result == dll
    assert len(compiler.calls) == 1, "one step, no closure"
    call = compiler.calls[0]
    assert call["cwd"] is None
    assert call["env"] == {"INCLUDE": "somewhere"}
    assert "/LD" in call["argv"]
    assert "/clang:-Werror" in call["argv"]
    assert str(tmp_path / "fifo.c") in call["argv"]
    assert dll.parent.is_dir(), "the output folder is created"


def test_ALX1544_P165_build_dll_two_steps_compiles_the_closure_then_links_it(
    tmp_path, toolchain, monkeypatch
):
    fake = FakeCompiler(make=["param.obj"])
    monkeypatch.setattr(hb, "run", lambda argv, cwd=None, env=None: fake(argv, cwd=cwd, env=env))
    dll = tmp_path / "cli.dll"
    hb.build_dll(
        toolchain,
        out=dll,
        strict=[tmp_path / "cli.c"],
        closure=[tmp_path / "param.c"],
        defines=["-DASSERTS_ON"],
        driver=hb.MSVC,
    )
    assert len(fake.calls) == 2
    first, second = fake.calls
    assert "/c" in first["argv"], "step 1 compiles only"
    assert "/w" in first["argv"], "step 1 has warnings off"
    assert first["cwd"] == dll.with_suffix(".closure"), "the default object folder"
    assert "/LD" in second["argv"], "step 2 links"
    assert str(dll.with_suffix(".closure") / "param.obj") in second["argv"], "the object is linked"
    # a define that changes behaviour must hold for both steps or the DLL mixes configurations
    assert "-DASSERTS_ON" in first["argv"]
    assert "-DASSERTS_ON" in second["argv"]


def test_ALX1544_P166_a_failing_step_raises_build_error_with_the_tool_output(
    tmp_path, toolchain, monkeypatch
):
    fake = FakeCompiler(fail_on="broken.c")
    monkeypatch.setattr(hb, "run", lambda argv, cwd=None, env=None: fake(argv, cwd=cwd, env=env))
    with pytest.raises(hb.BuildError) as caught:
        hb.build_dll(toolchain, out=tmp_path / "x.dll", strict=[tmp_path / "broken.c"])
    assert "x.dll build failed (rc=1)" in str(caught.value)
    assert "compiler said no" in str(caught.value)
    assert "line 7" in str(caught.value)

    fake.fail_on = "closure.c"
    with pytest.raises(hb.BuildError, match=r"y\.dll closure compile failed"):
        hb.build_dll(
            toolchain,
            out=tmp_path / "y.dll",
            strict=[tmp_path / "ok.c"],
            closure=[tmp_path / "closure.c"],
            obj_dir=tmp_path / "objs",
        )


def test_ALX1544_P167_build_exe_drops_the_shared_flag_and_run_exe_uses_the_build_env(
    tmp_path, toolchain, compiler
):
    exe = tmp_path / "smoke.exe"
    assert hb.build_exe(toolchain, out=exe, sources=[tmp_path / "smoke.c"]) == exe
    argv = compiler.calls[0]["argv"]
    assert "/LD" not in argv
    assert "-shared" not in argv

    hb.run_exe(toolchain, exe, "--quick", extra_env={"ASAN_OPTIONS": "abort_on_error=1"})
    last = compiler.calls[-1]
    assert last["argv"] == [str(exe), "--quick"]
    assert last["env"] == {"INCLUDE": "somewhere", "ASAN_OPTIONS": "abort_on_error=1"}

    hb.run_exe(toolchain, exe)
    assert compiler.calls[-1]["env"] == {"INCLUDE": "somewhere"}, "no extra environment, no change"

    gnu_exe = hb.build_exe(toolchain, out=exe, sources=[tmp_path / "s.c"], driver=hb.GNU)
    assert "-shared" not in compiler.calls[-1]["argv"]
    assert gnu_exe == exe


def test_ALX1544_P167_run_spawns_the_step_with_its_folder_and_its_environment(
    tmp_path, monkeypatch
):
    seen: dict[str, Any] = {}

    def fake_run(argv, **kw):
        seen.update({"argv": argv, **kw})
        return subprocess.CompletedProcess(argv, 0, stdout="out", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = hb.run(["clang", "x.c"], cwd=tmp_path, env={"INCLUDE": "X"})
    assert result.returncode == 0
    assert seen["argv"] == ["clang", "x.c"]
    assert seen["cwd"] == str(tmp_path)
    assert seen["env"] == {"INCLUDE": "X"}
    assert seen["check"] is False, "the caller decides what a non-zero exit means"

    hb.run(["clang", "--version"])
    assert seen["cwd"] is None
    assert seen["env"] is None


def test_ALX1544_P168_tool_locations_come_from_the_environment_variables(tmp_path, monkeypatch):
    llvm = tmp_path / "llvm"
    llvm.mkdir()
    clang = llvm / ("clang.exe" if hb.WINDOWS else "clang")
    clang.write_text("", encoding="ascii")
    monkeypatch.setenv("ALX_LLVM_DIR", str(llvm))
    tc = hb.Toolchain()
    assert tc.llvm_dir() == llvm
    assert tc.llvm("clang") == clang
    assert tc.compiler(hb.GNU) == clang

    with pytest.raises(FileNotFoundError, match="ALX_LLVM_DIR"):
        tc.llvm("llvm-cov")

    monkeypatch.setenv("ALX_LLVM_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(FileNotFoundError, match="ALX_LLVM_DIR"):
        hb.Toolchain().llvm_dir()

    gcc = tmp_path / "arm-none-eabi-gcc"
    gcc.write_text("", encoding="ascii")
    monkeypatch.setenv("ALX_ARMGCC", str(gcc))
    monkeypatch.setenv("ALX_CPPCHECK", str(gcc))
    assert hb.Toolchain().armgcc() == gcc
    assert hb.Toolchain().cppcheck() == gcc


def test_ALX1544_P169_the_build_environment_is_vcvars_over_the_process_environment(
    tmp_path, monkeypatch
):
    bat = tmp_path / "vcvars64.bat"
    bat.write_text("@echo off", encoding="ascii")
    seen: list[str] = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="INCLUDE=X\nLIB=Y\nnot a pair\n=novar\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    env = hb.build_environment({"PATH": "p", "INCLUDE": "old"}, bat)
    assert env == {"PATH": "p", "INCLUDE": "X", "LIB": "Y"}, "vcvars wins, junk lines are skipped"
    assert str(bat) in seen[0]
    assert hb.build_environment({"PATH": "p"}, None) == {"PATH": "p"}, "no batch file, no change"

    # captured once: the second call to a Toolchain does not run vcvars again
    tc = hb.Toolchain()
    monkeypatch.setattr(tc, "vcvars", lambda: bat)
    monkeypatch.setattr(hb, "WINDOWS", True)
    first = tc.environment()
    runs = len(seen)
    assert tc.environment() == first
    assert len(seen) == runs, "vcvars ran once"


def test_ALX1544_P169_vcvars_is_found_through_vswhere_and_says_what_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
    tc = hb.Toolchain()
    with pytest.raises(FileNotFoundError, match="vswhere not found"):
        tc.vcvars()

    vswhere = tmp_path / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    vswhere.parent.mkdir(parents=True)
    vswhere.write_text("", encoding="ascii")
    studio = tmp_path / "VS2022"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=f"{studio}\n"),
    )
    with pytest.raises(FileNotFoundError, match=r"vcvars64\.bat not found"):
        tc.vcvars()

    bat = studio / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    bat.parent.mkdir(parents=True)
    bat.write_text("@echo off", encoding="ascii")
    assert tc.vcvars() == bat


def test_ALX1544_P169_the_sanitizer_runtime_comes_from_clangs_own_resource_dir(
    tmp_path, monkeypatch
):
    llvm = tmp_path / "llvm"
    llvm.mkdir()
    (llvm / ("clang.exe" if hb.WINDOWS else "clang")).write_text("", encoding="ascii")
    monkeypatch.setenv("ALX_LLVM_DIR", str(llvm))
    resource = tmp_path / "resource"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=f"{resource}\n"),
    )
    tc = hb.Toolchain()
    assert tc.resource_dir() == resource
    runtime = tc.asan_runtime()
    assert runtime.parent.parent == resource / "lib"
    assert "asan" in runtime.name
