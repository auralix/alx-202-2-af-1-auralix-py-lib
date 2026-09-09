# SPDX-License-Identifier: MIT
"""C style gate: two mechanical rules a compiler cannot state, checked on the given C sources.

Rule 1, NO TERNARY OPERATOR. Outside comments, strings and character literals, ``?`` has exactly
one meaning in C: the conditional operator. It hides a branch inside an expression, where a reader
skims and a coverage report cannot point at the arm that never ran. Write ``if`` / ``else``.

Rule 2, DOXYGEN TAG ALIGNMENT. Inside a ``/** ... */`` block the tag lines are read as a table, so
they must look like one: fields are separated by TABS only (tab stop 4, as in .editorconfig), every
name/value starts in the same column within the block, and every description starts in the same
column within the block. ``@brief``, ``@note``, ``@return`` and ``@details`` carry a description
only; ``@param`` and ``@retval`` carry a name and then a description.

Both rules are checked by scanning, not by parsing: no compiler, no include path, no build. Usage::

    python -m alx.verify.c_style <file> [<file> ...] [--out report.txt]

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

TABSTOP = 4
DESC_ONLY_TAGS = frozenset({"brief", "note", "return", "details"})
NAMED_TAGS = frozenset({"param", "retval"})

_TAG = re.compile(r"^(\s*\*\s*)(@\w+(?:\[[^\]]*\])?)(.*)$")

# The scanner states of rule 1: plain code, // to end of line, /* */, "..." and '...'.
_CODE, _LINE_COMMENT, _BLOCK_COMMENT, _STRING, _CHAR = range(5)


def find_ternaries(text: str) -> list[int]:
    """Return the line numbers of every ``?`` that is code, skipping comments and literals."""
    state = _CODE
    escape = False
    line = 1
    hits: list[int] = []
    i, size = 0, len(text)
    while i < size:
        char = text[i]
        nxt = text[i + 1] if i + 1 < size else ""
        if char == "\n":
            line += 1
            if state == _LINE_COMMENT:
                state = _CODE
        elif state == _CODE:
            if char == "/" and nxt == "/":
                state, i = _LINE_COMMENT, i + 1
            elif char == "/" and nxt == "*":
                state, i = _BLOCK_COMMENT, i + 1
            elif char == '"':
                state = _STRING
            elif char == "'":
                state = _CHAR
            elif char == "?":
                hits.append(line)
        elif state == _BLOCK_COMMENT:
            if char == "*" and nxt == "/":
                state, i = _CODE, i + 1
        elif state in (_STRING, _CHAR):
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif (state == _STRING and char == '"') or (state == _CHAR and char == "'"):
                state = _CODE
        i += 1
    return hits


def _leading_space(text: str) -> str:
    """Return the whitespace ``text`` starts with (empty when a visible character starts it)."""
    return text[: len(text) - len(text.lstrip())]


def _tag_line(path: str, raw: str, number: int) -> tuple[str | None, int, int | None, list[str]]:
    """Measure one doxygen tag line: ``(kind, name_or_desc_column, desc_column, findings)``.

    ``kind`` is ``"desc"`` for a description-only tag, ``"name"`` for a named one and None when the
    line carries no tag this gate knows. Columns are measured on the tab-expanded line.
    """
    match = _TAG.match(raw)
    if match is None:
        return None, 0, None, []
    token: str = match.group(2)
    rest: str = match.group(3)
    tag = token[1:].split("[", maxsplit=1)[0]
    if tag not in DESC_ONLY_TAGS and tag not in NAMED_TAGS:
        return None, 0, None, []

    findings: list[str] = []
    if " " in _leading_space(rest):
        findings.append(f"{path}:{number}: spaces in field separator after {token} (tabs only)")

    expanded = raw.expandtabs(TABSTOP)
    after_tag = expanded.index(token) + len(token)
    tail = expanded[after_tag:]
    field = tail.lstrip()
    if not field:
        return None, 0, None, findings

    first_column = after_tag + (len(tail) - len(field))
    if tag in DESC_ONLY_TAGS:
        return "desc", first_column, None, findings

    name = field.split()[0]
    after_name = first_column + len(name)
    tail2 = expanded[after_name:]
    description = tail2.lstrip()
    if not description:
        return "name", first_column, None, findings

    raw_after_name = raw[raw.index(name, raw.index(token)) + len(name) :]
    if " " in _leading_space(raw_after_name):
        findings.append(f"{path}:{number}: spaces in field separator after '{name}' (tabs only)")
    return "name", first_column, after_name + (len(tail2) - len(description)), findings


def _doc_block(path: str, lines: list[str], start: int) -> tuple[int, list[str]]:
    """Check the block whose first body line is ``lines[start]``; return its end index + findings.

    The end index is the ``*/`` line, or the end of the file when the block is never closed.
    """
    columns: dict[str, dict[int, list[int]]] = {"name/value": {}, "description": {}}
    findings: list[str] = []
    i = start
    while i < len(lines) and "*/" not in lines[i]:
        kind, first, second, line_findings = _tag_line(path, lines[i], i + 1)
        findings += line_findings
        if kind == "desc":
            columns["description"].setdefault(first, []).append(i + 1)
        elif kind == "name":
            columns["name/value"].setdefault(first, []).append(i + 1)
            if second is not None:
                columns["description"].setdefault(second, []).append(i + 1)
        i += 1
    for label, seen in columns.items():
        if len(seen) > 1:
            detail = "; ".join(f"col {c} -> line(s) {v}" for c, v in sorted(seen.items()))
            findings.append(f"{path}:{start}: doc block {label} columns not aligned: {detail}")
    return i, findings


def check_text(path: str, text: str) -> list[str]:
    """Return every finding in one already-read source: the ternaries first, then the doc blocks."""
    findings = [f"{path}:{line}: ternary operator (write if/else)" for line in find_ternaries(text)]
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("/**"):
            i, block_findings = _doc_block(path, lines, i + 1)
            findings += block_findings
        i += 1
    return findings


def check(files: Iterable[str | Path]) -> list[str]:
    """Return the findings of every file; a file that is not pure ASCII is itself a finding."""
    findings: list[str] = []
    for item in files:
        path = Path(item)
        try:
            text = path.read_text(encoding="ascii")
        except UnicodeDecodeError as ex:
            findings.append(f"{path}:1: not ASCII ({ex.reason} at byte {ex.start})")
            continue
        findings += check_text(str(path), text)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.c_style", description=__doc__)
    parser.add_argument("files", nargs="+", help="the C sources and headers to check")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    findings = check(args.files)
    verdict = (
        f"FAIL ({len(findings)} finding(s))" if findings else f"PASS ({len(args.files)} files)"
    )
    report = "\n".join([f"C STYLE GATE: {verdict}", *findings]) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
