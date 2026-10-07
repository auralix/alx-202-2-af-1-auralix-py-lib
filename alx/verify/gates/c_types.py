# SPDX-License-Identifier: MIT
"""Type gate: decision 12's closed list of declared types, read from clang's AST.

Auralix C code declares only ``uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t
float double bool char``, the enums, structs and typedefs of its own headers - the names that
start with the repository's prefix (``Alx`` by default) - and ``void``. ``const``, ``*``, ``**``
and ``[n]`` are qualifiers on any of them. Never declared: ``int unsigned short long signed
_Bool size_t ssize_t ptrdiff_t intptr_t uintptr_t wchar_t``, ``signed``/``unsigned char`` and
``long double``; a foreign type from libc or a vendor lives only inside the cast that turns it
into an Auralix type.

Every variable, parameter, field, function return and typedef that a checked file declares is
compared; the qualifiers are stripped first. The named exceptions, closed:

* ``int main`` with ``argc`` and ``argv``;
* a function that implements a foreign interface - its name does not start with the prefix, the
  vendor or HAL callback, the RTOS hook, a syscall - keeps the signature it was given;
* a comparator handed to ``qsort``: ``int name(const void*, const void*)``.

The declarations come from ``clang -Xclang -ast-dump=json -fsyntax-only`` with each file's own
command from a compile database, so a file is checked as its build sees it. Usage::

    python -m alx.verify.gates.c_types <compile_commands.json> [--file F]... [--root DIR]
        [--exclude NAME]... [--prefix Alx]... [--clang PATH] [--out report.txt]

Without ``--file`` every file of the database is checked; ``--root`` adds every C file under a
folder, the headers among them, and ``--exclude`` leaves out a folder or a file by its name. Exit
code 0 = PASS, 1 = FAIL. Every finding is one ``<file>:<line>: <kind> <name>: <type>: <what>``
line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

ALLOWED = frozenset({
    "uint8_t", "uint16_t", "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t", "int64_t",
    "float", "double", "bool", "char", "void",
})  # fmt: skip
NEVER = frozenset({
    "int", "unsigned", "short", "long", "signed", "_Bool", "size_t", "ssize_t", "ptrdiff_t",
    "intptr_t", "uintptr_t", "wchar_t",
})  # fmt: skip
QUALIFIERS = frozenset({"const", "volatile", "restrict", "__restrict", "struct", "enum", "union"})
KINDS = {
    "VarDecl": "variable", "ParmVarDecl": "parameter", "FieldDecl": "field",
    "FunctionDecl": "function", "TypedefDecl": "typedef",
}  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z_]\w*")
_UNNAMED = re.compile(r"\((?:unnamed|anonymous)\b.*?:\d+:\d+\)")


def key(path: str | Path) -> str:
    """Return the form a file is compared in: absolute, and in the platform's case."""
    return os.path.normcase(str(Path(path).resolve()))


def type_names(qual_type: str) -> list[str]:
    """Return the type names in a clang type spelling, qualifiers, pointers and arrays stripped.

    An unnamed struct, union or enum is spelled with the place it is written - ``union (unnamed at
    alxX.c:219:2)`` - and has no name to judge; its fields are declarations of their own.
    """
    return [t for t in _TOKEN.findall(_UNNAMED.sub("", qual_type)) if t not in QUALIFIERS]


def judge(names: Iterable[str], prefixes: Iterable[str]) -> str | None:
    """Return what is wrong with a declared type's names, or None when every one is on the list."""
    names = list(names)
    never = [n for n in names if n in NEVER]
    if never:
        return f"never declared: {' '.join(never)}"
    foreign = [n for n in names if n not in ALLOWED and not n.startswith(tuple(prefixes))]
    if foreign:
        return f"a foreign type: {' '.join(foreign)}"
    return None


def exempt_function(node: dict[str, Any], prefixes: Iterable[str]) -> bool:
    """Whether a function keeps the signature it was given (the named exceptions)."""
    name = node.get("name", "")
    if name == "main":
        return True
    own = (*prefixes, *(p.lower() for p in prefixes))
    if not name.startswith(own):
        return True  # it implements a foreign interface: a HAL callback, an RTOS hook, a syscall
    qual = node.get("type", {}).get("qualType", "")
    return re.fullmatch(r"int\s*\(const void \*, const void \*\)", qual) is not None


def _carry(loc: dict[str, Any], state: dict[str, Any]) -> None:
    """Advance the carried file and line over one location, in the order clang printed it."""
    for piece in (loc["spellingLoc"], loc["expansionLoc"]) if "expansionLoc" in loc else (loc,):
        if "file" in piece:
            state["file"] = piece["file"]
        if "line" in piece:
            state["line"] = piece["line"]


def walk(node: dict[str, Any], state: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str, int]]:
    """Yield (node, file, line) for every node in document order.

    Clang prints a file and a line only when they differ from the location it printed last: the
    node's own, then its range's begin and end, each inside a macro as spelling then expansion. So
    all of them are followed in that order, and a node is placed where its own location was
    expanded.
    """
    _carry(node.get("loc", {}), state)
    file, line = state.get("file", ""), int(state.get("line", 0))
    span = node.get("range", {})
    _carry(span.get("begin", {}), state)
    _carry(span.get("end", {}), state)
    yield node, file, line
    for child in node.get("inner", []):
        yield from walk(child, state)


def check_ast(
    ast: dict[str, Any], files: set[str], prefixes: list[str], directory: str | Path = "."
) -> list[tuple[str, int, str]]:
    """Return (file, line, finding) for every declaration of a checked file that breaks the list.

    A file clang names relative is relative to ``directory``, the folder clang ran in.
    """
    found = []
    exempt_params: set[int] = set()
    for node, file, line in walk(ast, {}):
        kind = node.get("kind")
        if kind not in KINDS or not file or node.get("isImplicit"):
            continue
        if key(Path(directory) / file) not in files:
            continue
        name = node.get("name", "")
        qual = node.get("type", {}).get("qualType", "")
        if kind == "FunctionDecl":
            if exempt_function(node, prefixes):
                exempt_params.update(id(c) for c in node.get("inner", []))
                continue
            qual = qual.split("(", 1)[0].strip()  # the return type; parameters are their own nodes
        if kind == "ParmVarDecl" and id(node) in exempt_params:
            continue
        what = judge(type_names(qual), prefixes)
        if what:
            found.append((file, line, f"{KINDS[kind]} {name}: {qual}: {what}"))
    return found


def ast_of(entry: dict[str, Any], clang: str) -> dict[str, Any]:
    """Run clang's AST dump with the file's own command; raises RuntimeError when clang fails."""
    argv = entry.get("arguments") or shlex.split(entry["command"], posix=False)
    argv = [a for a in argv[1:] if a != "-c" and not a.startswith(("-o", "/Fo"))]
    argv = [
        clang,
        *[a for a in argv if a != entry["file"]],
        "-fsyntax-only",
        "-Xclang",
        "-ast-dump=json",
        entry["file"],
    ]
    result = subprocess.run(  # noqa: S603 - the argv is the compile database's own
        argv, cwd=entry.get("directory"), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        msg = f"clang failed on {entry['file']}:\n{result.stderr[-2000:]}"
        raise RuntimeError(msg)
    ast: dict[str, Any] = json.loads(result.stdout)
    return ast


def entry_file(entry: dict[str, Any]) -> Path:
    """Return a compile database entry's file; a relative one is relative to its directory."""
    return Path(entry.get("directory", ".")) / str(entry["file"])


def checked_files(
    entries: list[dict[str, Any]], root: str | None, exclude: Iterable[str]
) -> set[str]:
    """Return the keys of the files to judge: the database's own and, with a root, every C file.

    A file is left out when its name is excluded or, under the root, a folder between the two is.
    """
    exclude = set(exclude)
    base = Path(root).resolve() if root else None
    files = [entry_file(e).resolve() for e in entries]
    if base is not None:
        files += [f.resolve() for f in base.rglob("*.[ch]")]
    kept = set()
    for file in files:
        parts = {file.name}
        if base is not None and file.is_relative_to(base):
            parts |= set(file.relative_to(base).parts)
        if not parts & exclude:
            kept.add(key(file))
    return kept


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(prog="python -m alx.verify.gates.c_types", description=__doc__)
    parser.add_argument("database", help="compile_commands.json")
    parser.add_argument("--file", action="append", default=[], help="a file to check; repeatable")
    parser.add_argument("--root", help="also check every C file under this folder")
    parser.add_argument("--exclude", action="append", default=[], help="a folder or file to skip")
    parser.add_argument("--prefix", action="append", default=[], help="own type prefix (Alx)")
    parser.add_argument("--clang", default="clang", help="the clang to run")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    prefixes = args.prefix or ["Alx"]
    entries = json.loads(Path(args.database).read_text(encoding="utf-8"))
    wanted = {key(f) for f in args.file}
    entries = [e for e in entries if not wanted or key(entry_file(e)) in wanted]
    checked = checked_files(entries, args.root, args.exclude)
    findings: set[str] = set()
    for entry in entries:
        ast = ast_of(entry, args.clang)
        for file, line, what in check_ast(ast, checked, prefixes, entry.get("directory", ".")):
            findings.add(f"{file}:{line}: {what}")
    verdict = "FAIL" if findings else "PASS"
    report = "\n".join([f"C TYPES GATE: {verdict} ({len(entries)} files)", *sorted(findings)])
    report += "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
