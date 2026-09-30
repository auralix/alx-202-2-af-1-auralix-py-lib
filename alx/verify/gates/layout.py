# SPDX-License-Identifier: MIT
"""Layout gate: a verification root holds the template's entries, and every file has its place.

The template is the same in every repository, relative to its verification root - the repository
root of a Python repository, ``Test/`` of a C one - and this module states it as data:

- ``REQUIRED`` files at the root, and a ``tests/`` tree.
- ``ROOT_DIRS``: the only folders at the root, besides a Python repository's import packages and
  hidden folders.
- ``ROLES``: C compiled for the host or into the target image sits directly in a folder named
  after its role, and each role folder holds one shape of file (``ROLE_FILES``): the folder is the
  role, the file's suffix repeats it - ``fakes/<prefix><Module>Fake.c``,
  ``helpers/<prefix><Module>TestHelpers.c`` (a part in its own file: ``..._TestHelpers_<Part>.c``),
  ``checks/<prefix><Subject>Check.c``, ``shim/<prefix>HostShim.c`` and ``.h``; a shim header that
  replaces a vendor header keeps the vendor's name, because the product's includes must find it.
- ``harness/``: ``__init__.py`` and the packages ``host/`` and ``target/``, nothing else; a module
  is ``<snake_case>.py`` and never ``test_*``, which is a test module's name.
- ``TESTS_DIRS``: the first level of ``tests/`` - the execution location in a C repository; in a
  Python repository one folder per sub-package of the import package it mirrors. Both may add
  ``framework/`` for the checks of the verification system itself and ``integration/``.
- Every Python file in ``tests/`` is ``test_<snake_case>.py``, ``__init__.py`` or
  ``conftest.py``, and every folder holding Python is a package. A framework test mirrors what it
  checks: a harness module, ``conftest``, ``noxfile``, a host role folder, or ``architecture`` for
  the rules that span files. Test data is in ``data/`` at the root, at any depth inside it, and
  nowhere in ``tests/``; every data file identifies itself (``alx.verify.gates.data_source``).

Only what Git tracks is checked: build outputs, caches and virtual environments are not the
repository's, so they are never a finding. Usage::

    python -m alx.verify.gates.layout <root> --kind c|python [--package alx ...] [--out report.txt]

Exit code 0 = PASS, 1 = FAIL. Every finding is one ``<path>: <what>`` line.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REQUIRED = ("noxfile.py", "pyproject.toml", "uv.lock")
ROOT_DIRS = frozenset({"tests", "harness", "host", "target", "config", "data", "build"})
ROLES = frozenset({"helpers", "fakes", "checks", "shim"})
ROLE_FILES = {
    "fakes": re.compile(r"^[a-z0-9]+[A-Z][A-Za-z0-9]*Fake\.c$"),
    "helpers": re.compile(r"^[a-z0-9]+[A-Z][A-Za-z0-9]*TestHelpers(?:_[A-Z][A-Za-z0-9]*)?\.c$"),
    "checks": re.compile(r"^[a-z0-9]+[A-Z][A-Za-z0-9]*Check\.c$"),
    "shim": re.compile(r"^(?:[a-z0-9]+HostShim\.[ch]|[A-Za-z0-9_]+\.h)$"),
}
ROLE_SHAPES = {
    "fakes": "<prefix><Module>Fake.c",
    "helpers": "<prefix><Module>TestHelpers.c or ..._TestHelpers_<Part>.c",
    "checks": "<prefix><Subject>Check.c",
    "shim": "<prefix>HostShim.c or .h, or the vendor header it replaces",
}
HARNESS_PACKAGES = frozenset({"host", "target"})
FRAMEWORK_FIXED = frozenset({"conftest", "noxfile", "architecture"})
TESTS_DIRS = {"c": frozenset({"host", "target"}), "python": frozenset()}
TESTS_COMMON = frozenset({"framework", "integration"})
TESTS_FILES = frozenset({"__init__.py", "conftest.py"})

_TEST_MODULE = re.compile(r"^test_[a-z0-9]+(?:_[a-z0-9]+)*\.py$")
_MODULE = re.compile(r"^[a-z][a-z0-9_]*\.py$")


def tracked(root: Path) -> list[str]:
    """Return the files Git tracks under ``root``, as POSIX paths relative to it."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell; a read-only git query
        ["git", "-C", str(root), "ls-files", "--", "."],  # noqa: S607 - the developer's own git
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _sub_packages(paths: list[PurePosixPath], packages: Iterable[str]) -> set[str]:
    """Return the first-level sub-packages of the import packages (folders with ``__init__.py``)."""
    names = set(packages)
    return {
        p.parts[1]
        for p in paths
        if len(p.parts) == 3 and p.parts[0] in names and p.name == "__init__.py"
    }


def check_paths(paths: Iterable[str], kind: str, packages: Iterable[str] = ()) -> list[str]:
    """Return every finding for the tracked files of one verification root."""
    files = [PurePosixPath(p) for p in paths]
    packages = tuple(packages)
    findings = [f"{name}: missing" for name in REQUIRED if PurePosixPath(name) not in files]
    if not any(f.parts[0] == "tests" for f in files if len(f.parts) > 1):
        findings.append("tests/: missing")

    allowed_tests = TESTS_DIRS[kind] | TESTS_COMMON
    if kind == "python":
        allowed_tests |= _sub_packages(files, packages)
    mirrors = (
        FRAMEWORK_FIXED
        | ROLES
        | {
            f.stem
            for f in files
            if f.parts[0] == "harness" and len(f.parts) == 3 and f.suffix == ".py"
        }
    )
    test_dirs: set[PurePosixPath] = set()
    for f in files:
        top = f.parts[0]
        if len(f.parts) == 1:
            continue
        if top not in ROOT_DIRS and top not in packages and not top.startswith("."):
            findings.append(f"{f}: {top}/ is not a folder of the template")
        elif top in ("host", "target"):
            findings += _c_file(f)
        elif top == "harness":
            findings += _harness_file(f)
        elif top == "tests":
            findings += _test_file(f, allowed_tests, mirrors)
            if f.suffix == ".py":  # a folder holding Python is a package; data is a finding above
                test_dirs.update(p for p in f.parents if p.parts and p.parts[0] == "tests")
    for folder in sorted(test_dirs):
        if folder / "__init__.py" not in files:
            findings.append(f"{folder}/: not a package (no __init__.py)")
    return findings


def _c_file(f: PurePosixPath) -> list[str]:
    """Return the findings for a file under ``host/`` or ``target/``: role folder and shape."""
    if len(f.parts) < 3 or f.parts[1] not in ROLES:
        return [f"{f}: {f.parts[0]}/ holds only {', '.join(sorted(ROLES))}/"]
    role = f.parts[1]
    if len(f.parts) > 3 or not ROLE_FILES[role].match(f.name):
        return [f"{f}: {f.parts[0]}/{role}/ holds only {ROLE_SHAPES[role]}, directly"]
    return []


def _harness_file(f: PurePosixPath) -> list[str]:
    """Return the findings for one file under ``harness/``: the two packages, snake_case modules."""
    shape = f"harness/ holds __init__.py and the packages {', '.join(sorted(HARNESS_PACKAGES))}/"
    if len(f.parts) == 2:
        return [] if f.name == "__init__.py" else [f"{f}: {shape}"]
    if len(f.parts) > 3 or f.parts[1] not in HARNESS_PACKAGES:
        return [f"{f}: {shape}"]
    if f.name != "__init__.py" and (not _MODULE.match(f.name) or f.name.startswith("test_")):
        return [f"{f}: a harness module is <snake_case>.py, never test_*"]
    return []


def _test_file(
    f: PurePosixPath, allowed: frozenset[str] | set[str], mirrors: frozenset[str] | set[str]
) -> list[str]:
    """Return the findings for one file under ``tests/``."""
    if len(f.parts) > 2 and f.parts[1] not in allowed:
        return [f"{f}: tests/{f.parts[1]}/ is not a folder of the template"]
    if f.suffix != ".py":
        return [f"{f}: test data belongs in data/ at the verification root"]
    if f.name not in TESTS_FILES and not _TEST_MODULE.match(f.name):
        return [f"{f}: a test module is test_<snake_case>.py"]
    if len(f.parts) == 3 and f.parts[1] == "framework" and f.name not in TESTS_FILES:
        subject = f.stem[5:]
        if not any(subject == m or subject.startswith(m + "_") for m in mirrors):
            return [
                f"{f}: a framework test mirrors a harness module, conftest, noxfile, a host role "
                "or architecture"
            ]
    return []


def check(root: Path, kind: str, packages: Iterable[str] = ()) -> list[str]:
    """Return every finding for the verification root ``root``, from what Git tracks there."""
    return check_paths(tracked(root), kind, packages)


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.gates.layout", description=__doc__)
    parser.add_argument("root", type=Path, help="the verification root")
    parser.add_argument("--kind", choices=sorted(TESTS_DIRS), required=True)
    parser.add_argument("--package", action="append", default=[], help="an import package")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    findings = check(args.root, args.kind, args.package)
    verdict = f"FAIL ({len(findings)} finding(s))" if findings else "PASS"
    report = "\n".join([f"LAYOUT GATE: {verdict}", *findings]) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
