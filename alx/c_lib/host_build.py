# SPDX-License-Identifier: MIT
"""Host build of C modules as DLLs, for the pytest + ctypes harness of any C repository.

A C repository tests its modules on the host: the real sources are compiled into one DLL per test
group and driven from pytest through ctypes. The mechanics of that build are the same everywhere -
find the toolchain, rebuild only when something is newer, write a compile database, compile the
closure with warnings off and the gated sources with the full warning set, link with an export
list, and do it again with the sanitizer or coverage flags. Only the lists differ per repository:
which sources, which defines, which ``.def`` file. This module holds the mechanics.

    from alx.c_lib import host_build as hb

    tc = hb.Toolchain()
    if hb.needs_build(dll, deps):
        hb.build_dll(tc, out=dll, strict=STRICT, closure=CLOSURE, includes=INC,
                     defines=ASSERTS, def_file=DEF, flags=["-O0", "-g"],
                     warnings=[*hb.WARNINGS, "-Werror"], obj_dir=build / "closure")

Two compile drivers, because both are in use: :data:`GNU` is clang with GNU-style flags (what a
conftest builds the dev DLL with) and :data:`MSVC` is clang-cl with MSVC-style flags (what the
sanitizer and coverage lanes use). The argv for each is built by the pure functions
:func:`compile_argv` and :func:`link_argv`, so what a lane will run can be inspected and asserted
without running a compiler.

Tool locations are machine configuration, never repository content: an environment variable names
the instance on this machine (``ALX_LLVM_DIR``, ``ALX_ARMGCC``, ``ALX_CPPCHECK``), the defaults are
the reference bench. A build that needs a missing tool fails naming the variable, it never skips.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from alx.verify.lanes import tool

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

WINDOWS = sys.platform == "win32"

GNU = "clang"
"""Driver: clang with GNU-style flags (``-std=``, ``-I``, ``-shared -o``, ``-Wl,/DEF:``)."""

MSVC = "clang-cl"
"""Driver: clang-cl with MSVC-style flags (``/clang:-std=``, ``/I``, ``/LD``, ``/link /DEF:``)."""

OBJECT_SUFFIX = {GNU: ".o", MSVC: ".obj"}
"""What each driver names the objects of a compile-only step."""

WARNINGS: tuple[str, ...] = (
    "-Wall", "-Wextra",
    "-Wshadow", "-Wstrict-prototypes", "-Wold-style-definition",
    "-Wmissing-prototypes", "-Wmissing-declarations", "-Wmissing-variable-declarations",
    "-Wredundant-decls", "-Wnested-externs", "-Wbad-function-cast",
    "-Wcast-qual", "-Wwrite-strings", "-Wundef", "-Wvla", "-Walloca",
    "-Wswitch-enum", "-Wswitch-default", "-Wenum-conversion",
    "-Wformat=2", "-Wfloat-equal", "-Wdouble-promotion", "-Wimplicit-fallthrough",
    "-Wnull-dereference", "-Wunused", "-Wunused-macros", "-Wno-unused-parameter",
)  # fmt: skip
"""The strict host warning set; a caller adds ``-Werror`` to make it a gate."""

UBSAN: tuple[str, ...] = ("-fsanitize=undefined", "-fno-sanitize-recover=undefined")
"""SANITIZE variant: undefined behaviour is a hard failure, not a printed note."""

ASAN_UBSAN: tuple[str, ...] = ("-fsanitize=address,undefined", "-fno-sanitize-recover=undefined")
"""SANITIZE variant: ASan on top of UBSan (an executable, not a DLL, on Windows)."""

PROFILE: tuple[str, ...] = ("-fprofile-instr-generate", "-fcoverage-mapping")
"""COVERAGE variant: the instrumentation llvm-profdata and llvm-cov read back."""

CRT_DEFINE = "-D_CRT_SECURE_NO_WARNINGS"
"""The MSVC CRT deprecation noise, off in every host build: the target has no MSVC CRT."""


class BuildError(RuntimeError):
    """A compile or link step exited non-zero; the message carries the tool's own output."""


class Toolchain:
    """Where the host tools are on this machine, and the environment clang-cl needs.

    Every location comes from an environment variable with the reference bench as the default, the
    same rule and the same message as ``alx.verify.lanes.tool``. Nothing is looked up in the
    constructor: a repository that builds only the FIFO group must not need arm-gcc installed.
    """

    def __init__(self) -> None:
        """Create the toolchain; the build environment is captured on the first build, once."""
        self._build_env: dict[str, str] | None = None

    # -- locations -------------------------------------------------------------------
    def llvm_dir(self) -> Path:
        """Return the folder holding clang and the LLVM tools (``ALX_LLVM_DIR``)."""
        return tool("ALX_LLVM_DIR", "C:/Program Files/LLVM/bin" if WINDOWS else "/usr/bin")

    def llvm(self, name: str) -> Path:
        """Return one tool out of :meth:`llvm_dir`, with the platform's executable suffix."""
        exe = self.llvm_dir() / (f"{name}.exe" if WINDOWS else name)
        if not exe.exists():
            msg = f"tool not found: {exe} (install LLVM or set ALX_LLVM_DIR)"
            raise FileNotFoundError(msg)
        return exe

    def compiler(self, driver: str = MSVC) -> Path:
        """Return the compiler of a driver: clang for :data:`GNU`, clang-cl for :data:`MSVC`."""
        if driver == GNU or not WINDOWS:
            return self.llvm("clang")
        return self.llvm("clang-cl")

    def armgcc(self) -> Path:
        """Return the analysis-only ARM compiler (``ALX_ARMGCC``), newer than any product one."""
        default = (
            "C:/SysGCC/arm-eabi-15-2-1/bin/arm-none-eabi-gcc.exe"
            if WINDOWS
            else "/usr/bin/arm-none-eabi-gcc"
        )
        return tool("ALX_ARMGCC", default)

    def cppcheck(self) -> Path:
        """Return the cppcheck binary (``ALX_CPPCHECK``)."""
        default = "C:/Program Files/Cppcheck/cppcheck.exe" if WINDOWS else "/usr/bin/cppcheck"
        return tool("ALX_CPPCHECK", default)

    # -- the MSVC build environment --------------------------------------------------
    def vcvars(self) -> Path:
        """Return ``vcvars64.bat`` of the newest Visual Studio, found through vswhere."""
        program_files = os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")
        vswhere = Path(program_files) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if not vswhere.exists():
            msg = f"vswhere not found: {vswhere} (Visual Studio with the C++ workload)"
            raise FileNotFoundError(msg)
        found = subprocess.run(  # noqa: S603 - fixed argv, no shell; vswhere only reports paths
            [str(vswhere), "-latest", "-property", "installationPath"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        bat = Path(found) / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        if not bat.exists():
            msg = f"vcvars64.bat not found under {found}"
            raise FileNotFoundError(msg)
        return bat

    def environment(self) -> dict[str, str]:
        """Return the environment a clang-cl build needs (INCLUDE, LIB, PATH), captured once.

        Running vcvars costs about a second, and every step of every group would pay it again.
        On anything but Windows this is the environment as it stands: there is no vcvars there.
        """
        if self._build_env is None:
            self._build_env = build_environment(os.environ, self.vcvars() if WINDOWS else None)
        return dict(self._build_env)

    # -- what the sanitizers need ----------------------------------------------------
    def resource_dir(self) -> Path:
        """Return clang's own resource directory (``-print-resource-dir``)."""
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell; clang only prints a path
            [str(self.llvm("clang")), "-print-resource-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(out)

    def asan_runtime(self) -> Path:
        """Return the compiler's OWN ASan runtime: it must shadow the one MSVC ships."""
        parts = (
            ("lib", "windows", "clang_rt.asan_dynamic-x86_64.dll")
            if WINDOWS
            else ("lib", "linux", "libclang_rt.asan-x86_64.so")
        )
        return self.resource_dir().joinpath(*parts)


def build_environment(base: Mapping[str, str], vcvars: Path | None) -> dict[str, str]:
    """Return ``base`` plus what ``vcvars`` sets; without a batch file, ``base`` unchanged."""
    env = dict(base)
    if vcvars is not None:
        env.update(_vcvars_variables(vcvars))
    return env


def _vcvars_variables(bat: Path) -> dict[str, str]:
    """Return the variables ``vcvars64.bat`` sets, by running it and reading ``set`` after it."""
    # shell=True is the point: vcvars64.bat sets variables in a shell and only `set` reports them.
    out = subprocess.run(  # noqa: S602 - a batch file the toolchain owns, no external input
        f'"{bat}" >nul 2>&1 && set',
        shell=True,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    values: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key:
            values[key] = value
    return values


def needs_build(target: Path, deps: Iterable[Path]) -> bool:
    """Whether ``target`` must be rebuilt: it is missing, or a dependency is newer or gone."""
    if not target.exists():
        return True
    stamp = target.stat().st_mtime
    for dep in deps:
        try:
            if dep.stat().st_mtime > stamp:
                return True
        except OSError:
            return True  # a dependency that disappeared: rebuild and let the compiler say so
    return False


def _include_flags(driver: str, includes: Iterable[Path | str]) -> list[str]:
    prefix = "-I" if driver == GNU else "/I"
    return [f"{prefix}{item}" for item in includes]


def _warning_flags(driver: str, warnings: Iterable[str]) -> list[str]:
    """Pass a GNU warning flag to the clang front-end, whichever driver is in front of it.

    Measured, and the reason this function exists: clang-cl reads ``-Wall`` as MSVC's ``/Wall``,
    which is ``-Weverything``, so the strict set became every warning clang has - the c-lib's
    alxCli.c then failed on ``-Wpadded`` and ``-Wdeclaration-after-statement``, which nobody asked
    for. ``/clang:`` hands the flag to the front-end unchanged, exactly as ``/clang:-std=gnu99``
    does for the dialect.
    """
    if driver == GNU:
        return list(warnings)
    return [f"/clang:{flag}" for flag in warnings]


def compile_argv(
    compiler: Path | str,
    sources: Iterable[Path | str],
    *,
    driver: str = MSVC,
    std: str = "gnu99",
    includes: Iterable[Path | str] = (),
    defines: Iterable[str] = (),
    flags: Iterable[str] = (),
) -> list[str]:
    """Return the argv of step 1: the closure sources to objects, with warnings OFF.

    The closure is code linked in but not yet gated by its own tests; its warnings are somebody
    else's task, and letting them fail this build would gate the wrong module.
    """
    std_flag = f"-std={std}" if driver == GNU else f"/clang:-std={std}"
    warnings_off = "-w" if driver == GNU else "/w"
    return [
        str(compiler),
        std_flag,
        *flags,
        warnings_off,
        CRT_DEFINE,
        *defines,
        *_include_flags(driver, includes),
        "-c" if driver == GNU else "/c",
        *[str(s) for s in sources],
    ]


def link_argv(
    compiler: Path | str,
    out: Path,
    sources: Iterable[Path | str],
    *,
    driver: str = MSVC,
    std: str = "gnu99",
    includes: Iterable[Path | str] = (),
    defines: Iterable[str] = (),
    flags: Iterable[str] = (),
    warnings: Iterable[str] = (),
    objects: Iterable[Path | str] = (),
    def_file: Path | str | None = None,
) -> list[str]:
    """Return the argv of step 2: the gated sources plus the closure objects into the DLL ``out``.

    ``warnings`` is the set the gated sources are held to (``[*WARNINGS, "-Werror"]`` for a dev
    build, nothing for an instrumented variant that the dev build already checked); the flags are
    written in GNU form and reach the clang front-end unchanged under either driver, see
    :func:`_warning_flags`. ``def_file`` is the export list; without one the DLL exports whatever
    the sources mark.
    """
    std_flag = f"-std={std}" if driver == GNU else f"/clang:-std={std}"
    argv = [
        str(compiler),
        *(["/LD"] if driver == MSVC else []),
        std_flag,
        *flags,
        *_warning_flags(driver, warnings),
        CRT_DEFINE,
        *defines,
        *_include_flags(driver, includes),
        *[str(s) for s in sources],
        *[str(o) for o in objects],
    ]
    if driver == GNU:
        argv += ["-shared", "-o", str(out)]
        if def_file is not None:
            argv.append(f"-Wl,/DEF:{def_file}")
        return argv
    argv += [f"/Fe:{out}", f"/Fo{out.parent}{os.sep}"]
    if def_file is not None:
        argv += ["/link", f"/DEF:{def_file}"]
    return argv


def compile_db(
    sources: Iterable[Path],
    arguments: Sequence[str],
    directory: Path,
) -> list[dict[str, object]]:
    """Return the compile database entries: one per source, the shared ``arguments`` plus it."""
    unique = list(dict.fromkeys(sources))
    return [
        {"directory": str(directory), "arguments": [*arguments, "-c", str(src)], "file": str(src)}
        for src in unique
    ]


def write_compile_db(
    path: Path,
    sources: Iterable[Path],
    arguments: Sequence[str],
    directory: Path | None = None,
) -> None:
    """Write ``compile_commands.json`` so clang-tidy and clangd see the flags of the real build."""
    directory = path.parent if directory is None else directory
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = compile_db(sources, arguments, directory)
    path.write_text(json.dumps(entries, indent=1), encoding="ascii")


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one build step and return its result; the caller decides what a failure means."""
    return subprocess.run(  # noqa: S603 - the argv is built by this module from the caller's lists
        list(argv),
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        check=False,
    )


def _step(what: str, argv: Sequence[str], cwd: Path | None, env: Mapping[str, str]) -> None:
    result = run(argv, cwd=cwd, env=env)
    if result.returncode != 0:
        msg = f"{what} failed (rc={result.returncode}):\n{result.stdout}\n{result.stderr}"
        raise BuildError(msg)


def build_dll(
    toolchain: Toolchain,
    *,
    out: Path,
    strict: Iterable[Path],
    closure: Iterable[Path] = (),
    includes: Iterable[Path | str] = (),
    defines: Iterable[str] = (),
    def_file: Path | None = None,
    flags: Iterable[str] = (),
    warnings: Iterable[str] = (),
    obj_dir: Path | None = None,
    driver: str = MSVC,
    std: str = "gnu99",
) -> Path:
    """Build ``out`` from ``strict`` (and ``closure``) and return it; raises ``BuildError``.

    With a closure this is the two-step recipe: the closure compiles to objects with warnings off
    into ``obj_dir`` (default ``<out>.closure`` next to the DLL), then the gated sources compile and
    link against those objects under ``warnings``. Without one it is a single command.

    ``defines`` reach BOTH steps: a define that changes behaviour (asserts on, as the product ships
    them) must hold for the closure too, or the DLL is built from two different configurations.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    env = toolchain.environment()
    compiler = toolchain.compiler(driver)
    closure = list(closure)
    objects: list[str] = []
    if closure:
        obj_dir = out.with_suffix(".closure") if obj_dir is None else obj_dir
        obj_dir.mkdir(parents=True, exist_ok=True)
        _step(
            f"{out.name} closure compile",
            compile_argv(
                compiler,
                closure,
                driver=driver,
                std=std,
                includes=includes,
                defines=defines,
                flags=flags,
            ),
            obj_dir,
            env,
        )
        objects = [str(o) for o in sorted(obj_dir.glob(f"*{OBJECT_SUFFIX[driver]}"))]
    _step(
        f"{out.name} build",
        link_argv(
            compiler,
            out,
            strict,
            driver=driver,
            std=std,
            includes=includes,
            defines=defines,
            flags=flags,
            warnings=warnings,
            objects=objects,
            def_file=def_file,
        ),
        None,
        env,
    )
    return out


def build_exe(
    toolchain: Toolchain,
    *,
    out: Path,
    sources: Iterable[Path],
    includes: Iterable[Path | str] = (),
    defines: Iterable[str] = (),
    flags: Iterable[str] = (),
    warnings: Iterable[str] = (),
    driver: str = MSVC,
    std: str = "gnu99",
) -> Path:
    """Build an executable (an ASan smoke needs one: the address sanitizer cannot live in a DLL)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = link_argv(
        toolchain.compiler(driver),
        out,
        sources,
        driver=driver,
        std=std,
        includes=includes,
        defines=defines,
        flags=flags,
        warnings=warnings,
    )
    argv = [a for a in argv if a not in ("/LD", "-shared")]
    _step(f"{out.name} build", argv, None, toolchain.environment())
    return out


def run_exe(
    toolchain: Toolchain,
    exe: Path,
    *args: str,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a built executable in the build environment; the caller judges the exit code."""
    env = toolchain.environment()
    if extra_env:
        env.update(extra_env)
    return run([str(exe), *args], cwd=cwd, env=env)
