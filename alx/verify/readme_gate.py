# SPDX-License-Identifier: MIT
"""README gate: every Markdown file under a root uses only the allowed heading levels and no rules.

The Auralix documentation shape: ``#`` is the title, ``##`` a chapter, ``####`` a sub-chapter;
``###`` and deeper levels are not used, headings are ATX (``#`` lines, never underlined with
``===`` or ``---``), and there are no horizontal rules (``---``, ``***``, ``___``) - chapters
separate the text. Fenced code blocks are not inspected. Tool and build folders are skipped;
vendor folders are excluded by the caller. Usage::

    python -m alx.verify.readme_gate <root> [--exclude <name-or-path>]... [--out report.txt]

Exit code 0 = PASS, 1 = FAIL; every offending line is reported as ``<file>:<line>: <finding>``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from alx.verify.ascii_gate import SKIP_DIRS, _excluded

if TYPE_CHECKING:
    from collections.abc import Iterable

ALLOWED_LEVELS = frozenset({1, 2, 4})
"""Heading levels a README may use: title, chapter, sub-chapter."""

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_HEADING = re.compile(r"^(#{1,6})(?:\s|$)")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_SETEXT = re.compile(r"^\s{0,3}=+\s*$")


def markdown_files(root: str | Path, exclude: Iterable[str] = ()) -> list[Path]:
    """Return the Markdown files under ``root``, minus skipped and excluded folders."""
    root = Path(root)
    exclude = tuple(exclude)
    files = []
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or _excluded(rel, exclude):
            continue
        if path.is_file() and path.suffix.lower() in MARKDOWN_SUFFIXES:
            files.append(path)
    return files


def check_text(text: str) -> list[tuple[int, str]]:
    """Return ``(line, finding)`` for every offending line of one Markdown text."""
    findings = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if level not in ALLOWED_LEVELS:
                allowed = ", ".join("#" * n for n in sorted(ALLOWED_LEVELS))
                findings.append((number, f"heading level {level} ({allowed} only)"))
        elif _RULE.match(line):
            findings.append(
                (number, "horizontal rule or setext underline (chapters separate the text)")
            )
        elif _SETEXT.match(line):
            findings.append((number, "setext underline (ATX headings only)"))
    return findings


def check(files: Iterable[Path]) -> list[str]:
    """Return one finding per offending line: ``<file>:<line>: <finding>``."""
    findings: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(f"{path}:{line}: {what}" for line, what in check_text(text))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.readme_gate", description=__doc__)
    parser.add_argument("root", help="folder to scan (recursively)")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME_OR_PATH",
        help="folder name anywhere in the tree, or a path relative to root; repeatable",
    )
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    files = markdown_files(args.root, args.exclude)
    findings = check(files)
    scope = f"{len(files)} files" + (
        f", excluded: {', '.join(args.exclude)}" if args.exclude else ""
    )
    lines = [f"README GATE: {'FAIL' if findings else 'PASS'} ({scope})", *findings]
    report = "\n".join(lines) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii", errors="replace")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
