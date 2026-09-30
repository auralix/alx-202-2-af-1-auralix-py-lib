# SPDX-License-Identifier: MIT
"""Layout gate: a verification root holds the template's entries, and every file has its place.

The template is the same in every repository, relative to its verification root - the repository
root of a Python repository, ``Test/`` of a C one - and this module states it as data:

- ``REQUIRED`` files at the root, and a ``tests/`` tree.
- ``ROOT_DIRS``: the only folders at the root, besides a Python repository's import packages and
  hidden folders.
- ``HOST_ROLES`` and ``TARGET_ROLES``: C compiled for the host or into the target image sits in
  a folder named after its role, never loose.
- ``TESTS_DIRS``: the first level of ``tests/`` - the execution location in a C repository; in a
  Python repository one folder per sub-package of the import package it mirrors. Both may add
  ``framework/`` for the checks of the verification system itself and ``integration/``.
- Every Python file in ``tests/`` is ``test_<snake_case>.py``, ``__init__.py`` or
  ``conftest.py``, and every folder holding Python is a package. Test data is in ``data/`` at
  the root, at any depth inside it, and nowhere in ``tests/``; every data file identifies
  itself (``alx.verify.data_source``).

Only what Git tracks is checked: build outputs, caches and virtual environments are not the
repository's, so they are never a finding. Usage::

    python -m alx.verify.layout <root> --kind c|python [--package alx ...] [--out report.txt]

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
HOST_ROLES = frozenset({"helpers", "fakes", "checks"})
TARGET_ROLES = frozenset({"helpers", "checks"})
TESTS_DIRS = {"c": frozenset({"host", "target"}), "python": frozenset()}
TESTS_COMMON = frozenset({"framework", "integration"})
TESTS_FILES = frozenset({"__init__.py", "conftest.py"})

_TEST_MODULE = re.compile(r"^test_[a-z0-9]+(?:_[a-z0-9]+)*\.py$")


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
    test_dirs: set[PurePosixPath] = set()
    for f in files:
        top = f.parts[0]
        if len(f.parts) == 1:
            continue
        if top not in ROOT_DIRS and top not in packages and not top.startswith("."):
            findings.append(f"{f}: {top}/ is not a folder of the template")
        elif top == "host" and (len(f.parts) < 3 or f.parts[1] not in HOST_ROLES):
            findings.append(f"{f}: host/ holds only {', '.join(sorted(HOST_ROLES))}/")
        elif top == "target" and (len(f.parts) < 3 or f.parts[1] not in TARGET_ROLES):
            findings.append(f"{f}: target/ holds only {', '.join(sorted(TARGET_ROLES))}/")
        elif top == "tests":
            findings += _test_file(f, allowed_tests)
            if f.suffix == ".py":  # a folder holding Python is a package; data is a finding above
                test_dirs.update(p for p in f.parents if p.parts and p.parts[0] == "tests")
    for folder in sorted(test_dirs):
        if folder / "__init__.py" not in files:
            findings.append(f"{folder}/: not a package (no __init__.py)")
    return findings


def _test_file(f: PurePosixPath, allowed: frozenset[str] | set[str]) -> list[str]:
    """Return the findings for one file under ``tests/``."""
    if len(f.parts) > 2 and f.parts[1] not in allowed:
        return [f"{f}: tests/{f.parts[1]}/ is not a folder of the template"]
    if f.suffix != ".py":
        return [f"{f}: test data belongs in data/ at the verification root"]
    if f.name not in TESTS_FILES and not _TEST_MODULE.match(f.name):
        return [f"{f}: a test module is test_<snake_case>.py"]
    return []


def check(root: Path, kind: str, packages: Iterable[str] = ()) -> list[str]:
    """Return every finding for the verification root ``root``, from what Git tracks there."""
    return check_paths(tracked(root), kind, packages)


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.layout", description=__doc__)
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
