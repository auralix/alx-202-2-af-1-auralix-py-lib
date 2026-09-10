# SPDX-License-Identifier: MIT
"""The C hooks of the MUTATE lane: is the mutant valid C, is it the same code, rebuild the binaries.

``alx.verify.mutation`` classifies mutants the same way in every language, but it can only answer
two of its questions by itself when the language is Python, where it parses the file. For C it
needs a compiler, and it asks through three hooks:

    check        does the mutant compile at all?          no  -> STILLBORN
    fingerprint  does it produce the same object code?    yes -> EQUIVALENT
    rebuild      rebuild the stale binaries under test    fails -> KILLED_COMPILE

The first two are the same in every C repository and live here whole. The third needs one thing
that is the consumer's: WHICH binaries exist, what each depends on and how each is built. That is
data the consumer already owns next to its source lists, so it is passed by name rather than
copied into a script - the repository declares a sequence of ``(target, dependencies, build)`` and
names it on the command line::

    python -m alx.c_lib.mutation_hooks check <file> -I <dir> -D <macro> [--std gnu99]
    python -m alx.c_lib.mutation_hooks fingerprint <file> --work <dir> -I <dir> -D <macro>
    python -m alx.c_lib.mutation_hooks rebuild --groups conftest:DLL_GROUPS --sys-path Test

so a consumer's MUTATE lane needs no file of its own. Exit code 0 = yes / done, 1 = no / failed;
``fingerprint`` prints the hash on stdout, which is what the driver compares.

Compiling here is always clang with GNU-style flags: these two hooks ask about the SOURCE, never
about the shipped binary, so the driver of the real build does not matter and one form keeps the
answers comparable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from alx.c_lib import host_build

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

ELF_MAGIC = b"\x7fELF"
"""First bytes of an ELF object; anything else compiled by clang here is COFF."""

WORK_STEM = "_tce"
"""Fixed name for the fingerprint scratch file - see :func:`object_fingerprint`."""


def _argv(
    toolchain: host_build.Toolchain,
    *tail: str,
    std: str,
    includes: Iterable[Path | str],
    defines: Iterable[str],
) -> list[str]:
    """Return the clang argv shared by both compiling hooks: warnings off, the consumer's flags."""
    return [
        str(toolchain.compiler(host_build.GNU)),
        f"-std={std}",
        "-w",
        host_build.CRT_DEFINE,
        *defines,
        *[f"-I{item}" for item in includes],
        *tail,
    ]


def syntax_ok(
    toolchain: host_build.Toolchain,
    path: Path | str,
    *,
    std: str = "gnu99",
    includes: Iterable[Path | str] = (),
    defines: Iterable[str] = (),
) -> bool:
    """Whether ``path`` is valid C: a front-end run, no code generated and nothing written.

    A mutant that does not compile is STILLBORN - the generator produced text, not a program, and
    it says nothing about the tests either way.
    """
    argv = _argv(toolchain, "-fsyntax-only", str(path), std=std, includes=includes, defines=defines)
    return _run(argv) == 0


def strip_object_timestamp(data: bytes) -> bytes:
    """Return the object bytes with the one nondeterministic field zeroed.

    A COFF object (Windows) carries a TimeDateStamp in bytes 4..8 of its file header, so two
    compiles of the same source differ there and nowhere else. An ELF object has no such field and
    is returned untouched.
    """
    if data.startswith(ELF_MAGIC):
        return data
    return data[:4] + bytes(4) + data[8:]


def object_fingerprint(
    toolchain: host_build.Toolchain,
    path: Path | str,
    work: Path,
    *,
    std: str = "gnu99",
    includes: Iterable[Path | str] = (),
    defines: Iterable[str] = (),
) -> str | None:
    """Return the hash of ``path``'s object code, or None when it does not compile.

    Two mutants with the same fingerprint as the original are EQUIVALENT for any test: the compiler
    produced the same code, so no test can tell them apart. This is trivial compile equivalence,
    not a proof of semantic equivalence, and it is the cheap half that keeps a survivor list short.

    The source is copied to a FIXED file name first: the name is embedded in the object, so
    compiling ``mutant_0007.c`` and ``mutant_0031.c`` would differ for that reason alone.
    """
    work.mkdir(parents=True, exist_ok=True)
    source, obj = work / f"{WORK_STEM}.c", work / f"{WORK_STEM}.o"
    source.write_bytes(Path(path).read_bytes())
    argv = _argv(
        toolchain, "-c", "-O1", "-o", str(obj), str(source),
        std=std, includes=includes, defines=defines,
    )  # fmt: skip
    if _run(argv) != 0:
        return None
    return hashlib.sha256(strip_object_timestamp(obj.read_bytes())).hexdigest()


def _run(argv: Sequence[str]) -> int:
    """Run one compiler call and return its exit code, in the environment as it stands.

    NOT in the build environment, deliberately. Capturing that means running vcvars64.bat, and a
    hook is a fresh process per mutant: measured 1.34 s per call with it against 0.22 s without,
    which over the ~1400 invocations of one C source is twenty-odd minutes of batch file. clang
    finds the MSVC headers by itself, so these two hooks never needed it - only the rebuild hook
    does, and it gets one through the consumer's own build recipe.
    """
    return host_build.run(argv).returncode


Group = tuple[Path, "Iterable[Path]", "Callable[[], None]"]


def load_groups(spec: str, sys_path: Iterable[Path | str] = ()) -> list[Group]:
    """Import ``module:attribute`` and return the ``(target, dependencies, build)`` list it holds.

    The consumer declares its groups where its source lists already live (a conftest), so the lane
    has no script of its own. ``sys_path`` entries are prepended so that module can be found.
    """
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        msg = f"--groups wants module:attribute, got {spec!r}"
        raise ValueError(msg)
    for entry in sys_path:
        sys.path.insert(0, str(entry))
    module = importlib.import_module(module_name)
    return list(getattr(module, attribute))


def rebuild_groups(groups: Iterable[Group]) -> str | None:
    """Build every stale target; None when all are current, else the first failure's text.

    A mutant that survives this step is in the binary the tests are about to load; one that does
    not is KILLED_COMPILE - the build rejected it, which is a kill by the strictest reviewer there
    is.
    """
    for target, deps, build in groups:
        if host_build.needs_build(Path(target), [Path(d) for d in deps]):
            try:
                build()
            except RuntimeError as ex:  # BuildError, and whatever a consumer's own recipe raises
                return f"rebuild failed ({Path(target).name}): {str(ex)[:1500]}"
    return None


def _add_compile_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="the C file to ask about")
    parser.add_argument("-I", "--include", action="append", default=[], metavar="DIR")
    parser.add_argument(
        "-D", "--define", action="append", default=[], metavar="MACRO",
        help="a macro, with or without the -D (as -DFOO, the C form argparse also accepts)",
    )  # fmt: skip
    parser.add_argument(
        "--std", default="gnu99", help="the dialect the target ships (default gnu99)"
    )


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.c_lib.mutation_hooks", description=__doc__)
    sub = parser.add_subparsers(dest="hook", required=True)
    _add_compile_options(sub.add_parser("check", help="exit 0 when the file compiles"))
    fingerprint = sub.add_parser("fingerprint", help="print the hash of the file's object code")
    _add_compile_options(fingerprint)
    fingerprint.add_argument("--work", required=True, help="scratch folder for the fixed-name copy")
    rebuild = sub.add_parser("rebuild", help="build every stale target of a group declaration")
    rebuild.add_argument("--groups", required=True, metavar="MODULE:ATTRIBUTE")
    rebuild.add_argument("--sys-path", action="append", default=[], metavar="DIR")
    args = parser.parse_args(argv)

    if args.hook == "rebuild":
        failure = rebuild_groups(load_groups(args.groups, args.sys_path))
        if failure is None:
            return 0
        sys.stdout.write(failure + "\n")
        return 1

    toolchain = host_build.Toolchain()
    # -DFOO and FOO both mean the macro FOO, so a consumer can hand over the flags it already has
    defines = [d if d.startswith("-D") else f"-D{d}" for d in args.define]
    options = {"std": args.std, "includes": args.include, "defines": defines}
    if args.hook == "check":
        return 0 if syntax_ok(toolchain, args.file, **options) else 1
    digest = object_fingerprint(toolchain, args.file, Path(args.work), **options)
    if digest is None:
        return 1
    sys.stdout.write(digest + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
