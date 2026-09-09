# SPDX-License-Identifier: MIT
"""ASCII gate: every text file under a root holds only bytes below 0x80 (the pure-ASCII rule).

Sources, tests, configuration and documentation stay plain ASCII so every tool, terminal and diff
shows them alike; non-ASCII content belongs in data files, never in code. Tool and build folders
are skipped; vendor folders are excluded by the caller. Usage::

    python -m alx.verify.ascii_gate <root> [--exclude <name-or-relative-path>]... [--out report.txt]

An exclude matches a folder name anywhere in the tree (``Ext``) or a path relative to the root
(``Test/gen``). Exit code 0 = PASS, 1 = FAIL; the first offending byte of each file is reported.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

TEXT_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".toml", ".cfg", ".ini", ".md", ".rst", ".txt", ".json", ".yml", ".yaml",
        ".ps1", ".c", ".h", ".def", ".cmake", ".xml", ".csv",
    }
)  # fmt: skip
TEXT_NAMES = frozenset(
    {"LICENSE", ".gitignore", ".gitattributes", ".editorconfig", ".python-version"}
)
SKIP_DIRS = frozenset(
    {
        ".git", ".venv", ".nox", ".tox", "build", "dist", "__pycache__", ".mypy_cache",
        ".ruff_cache", ".hypothesis", ".pytest_cache", "node_modules",
    }
)  # fmt: skip


def _excluded(rel: Path, exclude: Iterable[str]) -> bool:
    parts = rel.parts
    posix = rel.as_posix()
    for item in exclude:
        norm = item.replace("\\", "/").strip("/")
        if norm in parts or posix == norm or posix.startswith(norm + "/"):
            return True
    return False


def text_files(root: str | Path, exclude: Iterable[str] = ()) -> list[Path]:
    """Return the text files under ``root`` (suffix or name), minus skipped and excluded folders."""
    root = Path(root)
    exclude = tuple(exclude)
    files = []
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):  # case-sensitive order
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or _excluded(rel, exclude):
            continue
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES):
            files.append(path)
    return files


def first_non_ascii(data: bytes) -> tuple[int, int] | None:
    """Return ``(line, byte)`` of the first byte above 0x7F; None when ``data`` is pure ASCII."""
    if data.isascii():
        return None
    for i, b in enumerate(data):
        if b > 0x7F:
            return data[:i].count(b"\n") + 1, b
    return None  # pragma: no cover - isascii() already said there is one


def check(files: Iterable[Path]) -> list[str]:
    """Return one finding per offending file: ``<file>:<line>: byte 0xNN``."""
    findings = []
    for path in files:
        hit = first_non_ascii(path.read_bytes())
        if hit is not None:
            line, byte = hit
            findings.append(f"{path}:{line}: byte 0x{byte:02X}")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.ascii_gate", description=__doc__)
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
    files = text_files(args.root, args.exclude)
    findings = check(files)
    scope = f"{len(files)} files" + (
        f", excluded: {', '.join(args.exclude)}" if args.exclude else ""
    )
    lines = [f"ASCII GATE: {'FAIL' if findings else 'PASS'} ({scope})", *findings]
    report = "\n".join(lines) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
