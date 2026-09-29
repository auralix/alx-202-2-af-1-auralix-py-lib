# SPDX-License-Identifier: MIT
"""Fake style gate: the convention every link-time fake of a C library module follows.

A fake is the host stand-in for one module that talks to hardware. It defines the module's own
API and the controls a test drives it with, and a consumer links it in place of the module - so
the library that owns the module owns the fake, and every fake has the same shape:

Rule 1, NAMED AFTER ITS MODULE. The file is ``alx<Module>Fake.c`` and includes ``alx<Module>.h``.
Rule 2, NO CONSUMER CODE. Every quoted include is a library header (``alx*.h``) and every angled
include a C standard header, so nothing a consumer defines - a call recorder, a product helper -
can be reached from a fake that has to link into every consumer.
Rule 3, ONE MODULE PER FAKE. Every function the file exports is the module's own API
(``Alx<Module>_...``) or a control of this fake (``Alx<Module>Fake_...``).
Rule 4, TYPED HANDLES. No control takes a ``void`` pointer: the handle is the module's own type,
so a caller passing the wrong object fails to compile.
Rule 5, A RESET FOR ITS STATE. A fake that keeps file-scope state defines
``Alx<Module>Fake_Reset`` (with whatever a reset needs, a clock's step for instance); a stateless
one needs none.
Rule 6, THE HEADER'S OWN FAMILY GUARD (with ``--headers``). A block that exists only for an MCU
family - a constructor, a register mirror - is guarded by a condition that appears verbatim in the
module's header or its MCU sub-headers, because the header decides where the module's flavour
exists: a blanket ``defined(ALX_STM32)`` breaks every family the header does not list.

Checked by scanning, like the C style gate: no compiler, no include path. Usage::

    python -m alx.verify.fake_style <fake.c> [...] [--headers <dir> ...] [--out report.txt]

Exit code 0 = PASS, 1 = FAIL. Every finding is one ``<file>:<line>: <what>`` line.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

STANDARD_HEADERS = frozenset(
    {
        "assert.h", "ctype.h", "float.h", "inttypes.h", "limits.h", "math.h", "stdarg.h",
        "stdbool.h", "stddef.h", "stdint.h", "stdio.h", "stdlib.h", "string.h",
    }
)  # fmt: skip
"""The C standard headers a fake may include besides the library's own."""

_NAME = re.compile(r"^alx([A-Z][A-Za-z0-9]*)Fake\.c$")
_INCLUDE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')
# A function definition or prototype at file scope: return type, name, open parenthesis.
_FUNCTION = re.compile(r"^(?!static\b)[A-Za-z_][\w \t*]*?\b(Alx\w+)\s*\(")
_VOID_HANDLE = re.compile(r"\b(Alx\w+Fake_\w+)\s*\(\s*(?:const\s+)?void\s*\*")
_STATE = re.compile(r"^static\s+[^()]*?\b\w+\s*(?:\[[^\]]*\]\s*)*(?:=|;)")
_FAMILY = re.compile(r"^\s*#\s*(?:if|elif)\b.*\bdefined\s*\(\s*ALX_STM32")


def _module_headers(module: str, header_dirs: Iterable[Path]) -> list[Path]:
    """Return the module's header and its MCU sub-headers, wherever the directories hold them."""
    found: list[Path] = []
    for folder in header_dirs:
        found += sorted(folder.glob(f"alx{module}.h")) + sorted(folder.glob(f"alx{module}_Mcu*.h"))
    return found


def check_text(path: str, text: str, header_dirs: Iterable[Path] | None = None) -> list[str]:
    """Return every finding in one already-read fake."""
    name = Path(path).name
    match = _NAME.match(name)
    if match is None:
        return [f"{path}:1: not named alx<Module>Fake.c"]
    module = match.group(1)
    lines = text.splitlines()
    findings: list[str] = []

    includes = [
        (number, found.group(1), found.group(2))
        for number, raw in enumerate(lines, 1)
        if (found := _INCLUDE.match(raw))
    ]
    if not any(kind == '"' and header == f"alx{module}.h" for _, kind, header in includes):
        findings.append(f"{path}:1: does not include its module header alx{module}.h")
    for number, kind, header in includes:
        where = f"{path}:{number}: includes"
        if kind == '"' and not header.startswith("alx"):
            findings.append(f"{where} {header}, which is not a library header")
        if kind == "<" and header not in STANDARD_HEADERS:
            findings.append(f"{where} <{header}>, which is not a C standard header")

    for number, raw in enumerate(lines, 1):
        function = _FUNCTION.match(raw)
        if function and not function.group(1).startswith((f"Alx{module}_", f"Alx{module}Fake_")):
            findings.append(
                f"{path}:{number}: {function.group(1)} is neither Alx{module}_ API nor an "
                f"Alx{module}Fake_ control"
            )
        handle = _VOID_HANDLE.search(raw)
        if handle:
            findings.append(f"{path}:{number}: {handle.group(1)} takes a void pointer handle")

    stateful = any(_STATE.match(raw) for raw in lines)
    if stateful and not re.search(rf"\bAlx{module}Fake_Reset\s*\(", text):
        findings.append(f"{path}:1: keeps file-scope state but defines no Alx{module}Fake_Reset")

    if header_dirs is not None:
        headers = _module_headers(module, header_dirs)
        if not headers:
            findings.append(f"{path}:1: no header alx{module}.h in the given directories")
        guards = {
            raw.strip()
            for header in headers
            for raw in header.read_text(encoding="utf-8", errors="replace").splitlines()
        }
        for number, raw in enumerate(lines, 1):
            if headers and _FAMILY.match(raw) and raw.strip() not in guards:
                own = f"alx{module}.h's own"
                findings.append(f"{path}:{number}: family guard is not one of {own}: {raw.strip()}")
    return findings


def check(files: Iterable[str | Path], header_dirs: Iterable[Path] | None = None) -> list[str]:
    """Return the findings of every file; a file that is not pure ASCII is itself a finding."""
    dirs = None if header_dirs is None else list(header_dirs)
    findings: list[str] = []
    for item in files:
        path = Path(item)
        try:
            text = path.read_text(encoding="ascii")
        except UnicodeDecodeError as ex:
            findings.append(f"{path}:1: not ASCII ({ex.reason} at byte {ex.start})")
            continue
        findings += check_text(str(path), text, dirs)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.fake_style", description=__doc__)
    parser.add_argument("files", nargs="+", help="the fakes to check")
    parser.add_argument("--headers", nargs="+", type=Path, help="where the module headers are")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    findings = check(args.files, args.headers)
    verdict = (
        f"FAIL ({len(findings)} finding(s))" if findings else f"PASS ({len(args.files)} files)"
    )
    report = "\n".join([f"FAKE STYLE GATE: {verdict}", *findings]) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
