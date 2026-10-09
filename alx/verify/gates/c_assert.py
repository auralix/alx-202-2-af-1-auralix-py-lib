# SPDX-License-Identifier: MIT
"""Assertion gate: decision 14 - an assertion or a trace evaluates no call.

A disabled assertion keeps its expression unevaluated, and a disabled trace its arguments, so a
call inside either runs in one configuration and not in another: the work it does, or the value
it reads, depends on a switch that is meant to change nothing but the checking. An assertion
checks only variables; the call runs first into a local, and the assertion checks the local.

The calls are found inside every ``<PREFIX>_..._ASSERT...(...)`` and ``<PREFIX>_..._TRACE...(...)``
of the given C sources, outside comments, literals and the macros' own ``#define`` lines. A
function-like macro spelled in capitals (``ALX_ARR_LEN(arr)``) is a constant expression, not a
call, and so is ``sizeof``. Usage::

    python -m alx.verify.gates.c_assert <file> [<file> ...] [--out report.txt]

Exit code 0 = PASS, 1 = FAIL. Every finding is one ``<file>:<line>: <macro>: a call: <name>()``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from alx.verify.gates.c_format import arguments, code_mask

MACRO = re.compile(r"\b(?P<name>[A-Z][A-Z0-9]*_[A-Z0-9_]*(?:ASSERT|TRACE)[A-Z0-9_]*)\s*\(")
CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
NOT_CALLS = frozenset({"sizeof", "defined", "if", "while", "for", "switch", "return"})


def scan(path: Path) -> list[str]:
    """Return the findings of one file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    mask = code_mask(text)
    code = "".join(c if keep else " " for c, keep in zip(text, mask, strict=True))
    findings: list[str] = []
    for m in MACRO.finditer(code):
        line_start = code.rfind("\n", 0, m.start()) + 1
        if code[line_start : m.start()].lstrip().startswith("#"):
            continue  # a macro's own definition
        args, _ = arguments(code, m.end() - 1)
        line = text.count("\n", 0, m.start()) + 1
        for name in CALL.findall(",".join(args)):
            if name in NOT_CALLS or name.isupper():
                continue
            findings.append(f"{path}:{line}: {m.group('name')}: a call: {name}()")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(
        prog="python -m alx.verify.gates.c_assert", description=__doc__
    )
    parser.add_argument("files", nargs="+", help="C sources to check")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    findings = [f for name in args.files for f in scan(Path(name))]
    verdict = "FAIL" if findings else "PASS"
    report = "\n".join([f"C ASSERT GATE: {verdict} ({len(args.files)} files)", *findings]) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
