# SPDX-License-Identifier: MIT
"""Format string gate: decision 12's table over every format string of the given C sources.

A format string is the first string argument of a trace macro (any ``<PREFIX>_<...>TRACE<...>``),
of ``AlxTrace_WriteFormat``, ``sprintf``, ``snprintf`` or ``sscanf``. Adjacent literals and the
PRI and SCN macros between them are one format. Every conversion must be one of:

* a PRI or SCN macro of a fixed-width integer spliced into the text (``"len %" PRIu32``), hex only
  through the uppercase ``PRIX``;
* a bare ``%d``, ``%c``, ``%s``, ``%p``, ``%f`` or ``%%``;
* with the flags ``-`` and ``0``, width digits and ``.precision``, which are formatting; ``%.*s``
  with an ``(int)`` length on the call is the one ``*`` allowed, decision 12's named exception;
* in ``sscanf`` only: ``%lf``, the one place a length modifier is right, and ``%s`` only bounded
  (``%31s``).

Never: ``%u %x %X %o %i %e %E %g %G %a %n``; a length modifier (``l ll h hh z j t L``) but
scanf's ``%lf``; the ``#`` flag; a ``*`` width; a lowercase ``PRIx``; ``PRIo``. An exemption names
a file and a call (``alxBoot.c:sscanf``), for a spot TV froze. A format that is not a literal - a
variable - is not checked. Usage::

    python -m alx.verify.gates.c_format <file> [<file> ...] [--exempt FILE:CALL]...
        [--out report.txt]

Exit code 0 = PASS, 1 = FAIL. Every finding is one ``<file>:<line>: <what>`` line.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

CALL = re.compile(
    r"\b(?P<name>[A-Z][A-Z0-9]*_[A-Z0-9_]*TRACE[A-Z0-9_]*|AlxTrace_WriteFormat|sprintf|snprintf"
    r"|sscanf)\s*\("
)
MACRO = re.compile(r"(?:PRI|SCN)[diouxX](?:8|16|32|64)")
ALLOWED_MACRO = re.compile(r"(?:PRI[duX]|SCN[du])(?:8|16|32|64)")
BARE = frozenset("dcspf")
CONVERSION = re.compile(
    r"%(?P<flags>[-+ #0]*)(?P<width>\*|\d+)?(?:\.(?P<prec>\*|\d*))?(?P<len>hh|h|ll|l|z|j|t|L)?"
    r"(?P<conv>[a-zA-Z%[])?"
)
_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_IDENT = re.compile(r"[A-Za-z_]\w*")


def code_mask(text: str) -> list[bool]:
    """Return, per character, whether it is code: False in comments, strings and char literals."""
    mask = [True] * len(text)
    i, size = 0, len(text)
    while i < size:
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = size if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = size if end < 0 else end + 2
        elif text[i] in "\"'":
            quote, end = text[i], i + 1
            while end < size and text[end] != quote:
                end += 2 if text[end] == "\\" else 1
            end = min(end + 1, size)
        else:
            i += 1
            continue
        for k in range(i, end):
            mask[k] = False
        i = end
    return mask


def arguments(text: str, open_paren: int) -> tuple[list[str], int]:
    """Split the call whose ``(`` is at ``open_paren`` into its top-level arguments."""
    depth, start, args = 0, open_paren + 1, []
    i = open_paren
    while i < len(text):
        char = text[i]
        if char in "\"'":
            quote = char
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                args.append(text[start:i])
                return [a.strip() for a in args], i
        elif char == "," and depth == 1:
            args.append(text[start:i])
            start = i + 1
        i += 1
    args.append(text[start:])  # a call cut off at the end of the text: its last argument too
    return [a.strip() for a in args], len(text)


def format_pieces(arg: str) -> list[str] | None:
    """Return a format argument as text pieces and macro names, or None when it is not literal.

    A macro name is returned as itself; a piece of text is returned with a leading quote, so the
    two can be told apart (``'"len %'``, ``'PRIu32'``).
    """
    pieces: list[str] = []
    i = 0
    while i < len(arg):
        if arg[i].isspace():
            i += 1
            continue
        lit = _LITERAL.match(arg, i)
        if lit:
            pieces.append('"' + lit.group(1))
            i = lit.end()
            continue
        ident = _IDENT.match(arg, i)
        if ident and MACRO.fullmatch(ident.group(0)):
            pieces.append(ident.group(0))
            i = ident.end()
            continue
        return None
    return pieces if any(p.startswith('"') for p in pieces) else None


def check_format(pieces: list[str], call: str, values: Sequence[str] = ()) -> list[str]:
    """Return what is wrong with one format, one line of text per finding.

    ``values`` are the call's arguments after the format: the length of a ``%.*s`` is read there,
    and it is ``(int)len`` on the call. A print's every conversion takes one value and a ``*`` one
    more before it; a value the call does not have is the compiler's business.
    """
    scanf = call == "sscanf"
    found: list[str] = []
    used = 0
    for n, piece in enumerate(pieces):
        if not piece.startswith('"'):
            if not ALLOWED_MACRO.fullmatch(piece):
                found.append(f"{piece}: only PRIu, PRId, PRIX, SCNu and SCNd macros")
            continue
        text = piece[1:]
        for conv in CONVERSION.finditer(text):
            if conv.group(0) == "%%":
                continue
            found += _check_conversion(conv, text, pieces, n, scanf=scanf)
            if scanf:
                continue
            used += conv.group("width") == "*"
            if conv.group("prec") == "*":
                length = values[used] if used < len(values) else "(int)"
                if conv.group("conv") == "s" and not length.startswith("(int)"):
                    found.append(f"{conv.group(0)!r}: its length is (int)len on the call")
                used += 1
            used += 1
    return found


def _check_conversion(
    conv: re.Match[str], text: str, pieces: list[str], n: int, *, scanf: bool
) -> list[str]:
    spec = conv.group(0)
    flags, width, prec = conv.group("flags"), conv.group("width"), conv.group("prec")
    length, letter = conv.group("len"), conv.group("conv")
    out: list[str] = []
    if letter is None:  # the conversion is the macro that follows the literal
        if conv.end() != len(text) or n + 1 >= len(pieces) or pieces[n + 1].startswith('"'):
            return [f"{spec!r}: a % with no conversion"]
        macro = pieces[n + 1]
        if scanf and not macro.startswith("SCN"):
            out.append(f"{spec}{macro}: SCN macros in sscanf")
        elif not scanf and macro.startswith("SCN"):
            out.append(f"{spec}{macro}: PRI macros in a print")
    elif letter not in BARE and not (scanf and letter == "f" and length == "l"):
        out.append(f"{spec!r}: not in the table - use a PRI/SCN macro or %d %c %s %p %f")
    if set(flags) - {"-", "0"}:
        out.append(f"{spec!r}: flags other than - and 0")
    if width == "*":
        out.append(f"{spec!r}: a * width")
    if prec == "*" and letter != "s":
        out.append(f"{spec!r}: a * precision is allowed only as %.*s")
    if length and not (scanf and letter == "f" and length == "l"):
        out.append(f"{spec!r}: a length modifier")
    if scanf and letter == "s" and width is None:
        out.append(f"{spec!r}: an unbounded %s in sscanf")
    return out


def scan(path: Path, exempt: Iterable[str] = ()) -> list[str]:
    """Return the findings of one file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    mask = code_mask(text)
    exempt = set(exempt)
    findings: list[str] = []
    for m in CALL.finditer(text):
        if not mask[m.start()]:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        if text[line_start : m.start()].lstrip().startswith("#"):
            continue  # a macro's own definition
        name = m.group("name")
        if f"{path.name}:{name}" in exempt:
            continue
        args, _ = arguments(text, m.end() - 1)
        formats = ((k, format_pieces(a)) for k, a in enumerate(args))
        k, pieces = next(((k, p) for k, p in formats if p is not None), (0, None))
        if pieces is None:
            continue
        line = text.count("\n", 0, m.start()) + 1
        what = check_format(pieces, name, args[k + 1 :])
        findings += [f"{path}:{line}: {name}: {w}" for w in what]
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(
        prog="python -m alx.verify.gates.c_format", description=__doc__
    )
    parser.add_argument("files", nargs="+", help="C sources to check")
    parser.add_argument(
        "--exempt",
        action="append",
        default=[],
        metavar="FILE:CALL",
        help="a call in a file TV froze (alxBoot.c:sscanf); repeatable",
    )
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    findings = [f for name in args.files for f in scan(Path(name), args.exempt)]
    verdict = "FAIL" if findings else "PASS"
    report = "\n".join([f"C FORMAT GATE: {verdict} ({len(args.files)} files)", *findings]) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
