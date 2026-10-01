# SPDX-License-Identifier: MIT
r"""Configuration matrix: every source compiled in every configuration that selects code.

A C repository configures its library through one header of ``#define`` switches, and a switch
that is off hides the code behind it from every compiler. This module makes that visible and
gates it. A MATRIX of configurations is run against every source: each row's configuration
header is synthesized from the repository's base header plus one FRAGMENT per axis, every source
compiles with ``-fsyntax-only`` under ``-Werror``, every diagnostic is recorded, and the
preprocessor says which source lines each row compiled. The union over the rows then names every
line of the repository that no configuration reaches.

The matrix is data in the repository, a TOML file (``Test/config/matrix.toml`` in a C library)::

    [platform.stm32f7]
    description = "STM32F7, Cortex-M7 with the double-precision FPU"
    includes = ["host/shim/stm32", "host/shim/cmsis"]
    defines = ["STM32F765xx"]
    flags = ["-mcpu=cortex-m7", "-mthumb", "-mfpu=fpv5-d16", "-mfloat-abi=hard"]

    [platform.host]
    description = "the PC the host lanes run on"
    host = true

    defines = ["LFS_CONFIG=alxLfsConfig.h"]
    includes = ["host/shim/freertos"]
    coverage = { library = ["trace-vrb-assert-bkpt-opt-none"] }

    [[exempt]]
    file = "*"
    guard = "__cplusplus"
    why = "the C++ linkage block of every header; the library is compiled as C"

    [[row]]
    id = "0017"
    toolchain = "gcc"
    platform = "stm32f7"
    features = "all"
    target = "app-debug"
    library = "trace-vrb-assert-bkpt-opt-dbg"
    description = "the debug build of an F7 application with every feature"

A row's name is ``<id>_<toolchain>_<platform>_<features>_<target>_<library>``: the id is allocated
once and never renumbered, the five words are the AXES, one per group of the configuration header.
Every word is a fragment ``<fragments>/<axis>/<word>.h`` of ``#define NAME [value]`` and
``#undef NAME`` lines, or ``#include "other.h"`` to compose fragments, in the style of a Kconfig
fragment. :func:`synthesize` applies the five in axis order to the base header, so a row's header
is the base with exactly that row's switches changed, readable and diffable. A ``platform`` table
says what a platform word means to a compiler; the top-level ``defines`` and ``includes`` hold for
every row (the build defines every product sets, the fakes of the features); the compilers
themselves (:class:`Compiler`) are machine configuration and come from the caller. A repository
configured through more than one header - a product configures its own modules in headers of their
own - names the others in :attr:`Matrix.headers`, and each operation goes to the header declaring
its name.

Per row, every source is compiled with ``-fsyntax-only`` and the compiler's warnings; a diagnostic
is a finding, a missing header and an undeclared identifier are named, and when a header is missing
the preprocessor is asked once more (``-M -MG``) for every header of that source it cannot find,
not only the first: together they are the work list of the fake that row needs. Then ``-E`` runs
once per source and its line markers say which lines of which repository file came through.

Coverage is measured by BRANCH, not by line, because a statement spread over several lines comes
out of the preprocessor on its first line only: a branch of an ``#if`` group was compiled by a row
when any line inside it came through, and a code line - not blank, not a comment, not a directive
- is covered when the branch holding it was compiled by some row, or it holds no branch and its
file was reached at all. Only the rows that compile everything count: an assert or a trace whose
macro is elided drops its arguments, so its line is not compiled in any sense that matters - the
top-level ``coverage`` table names, per axis, the words whose rows are measured. A line no row
covers is reported with the directive that hid it; an ``exempt`` entry (a file pattern, an
identifier of the hiding directive and the reason) explains it instead. A finding the repository
decided to keep - a frozen line, a message a build prints on purpose - is named by an ``accept``
entry (a file pattern, a text of the message and the reason): it is reported with its reason and
fails no row. ``report.json`` holds
everything and ``report.md`` says it for a person; :mod:`alx.verify.gates.configs` fails on any
finding, any missing header or identifier, any line no row compiled, and any run that was not the
whole matrix.
"""

from __future__ import annotations

import datetime
import fnmatch
import json
import os
import re
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

AXES: tuple[str, ...] = ("toolchain", "platform", "features", "target", "library")
"""The five axes = the five groups of the configuration header, in the order of a row's name."""

WARNINGS: tuple[str, ...] = ("-Wall", "-Wextra", "-Wformat=2", "-Werror")
"""The warning set every row compiles under unless the caller names its own."""

GCC_DIAGNOSTICS: tuple[str, ...] = (
    "-fno-diagnostics-show-caret",
    "-fdiagnostics-color=never",
    "-fmax-errors=0",
)
"""GCC's diagnostics in the one-line form :func:`parse_diagnostics` reads, every one of them.

Spelled the way GCC 10 already understands, because a product may build with it;
``-fdiagnostics-plain-output`` says the same from GCC 11 on.
"""

CLANG_DIAGNOSTICS: tuple[str, ...] = (
    "-fno-caret-diagnostics",
    "-fno-color-diagnostics",
    "-ferror-limit=0",
)
"""Clang's diagnostics in the same form; ``-ferror-limit=0`` lifts its default stop at 20."""

PASS, FAIL, NO_SHIM = "PASS", "FAIL", "NO SHIM"
SCHEMA = 1
FINDINGS_KEPT = 200  # findings of one row quoted in report.json; findings.txt holds them all

_ID = re.compile(r"^[0-9]{4,6}$")
_WORD = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ROW_KEYS = ("id", *AXES, "description")
_PLATFORM_KEYS = ("description", "host", "includes", "defines", "flags")
_EXEMPT_KEYS = ("file", "guard", "why")
_ACCEPT_KEYS = ("file", "text", "why")
_DEFINE = re.compile(r"^(?P<off>\s*//\s*)?#define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<rest>.*)$")
_FRAGMENT_DEFINE = re.compile(r"^#define\s+(?P<name>[A-Za-z_]\w*)(?:\s+(?P<value>.*\S))?\s*$")
_FRAGMENT_UNDEF = re.compile(r"^#undef\s+(?P<name>[A-Za-z_]\w*)\s*$")
_FRAGMENT_INCLUDE = re.compile(r'^#include\s+"(?P<file>[^"]+)"\s*$')
_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<kind>fatal error|error|warning): (?P<text>.*)$"
)
_MISSING_HEADER = re.compile(
    r"^(?:'(?P<clang>[^']+)' file not found|(?P<gcc>[^:\s]+): No such file)"
)
_UNDECLARED = (
    re.compile(r"'(?P<name>[A-Za-z_]\w*)' undeclared"),
    re.compile(r"implicit declaration of function '(?P<name>[A-Za-z_]\w*)'"),
    re.compile(r"call to undeclared function '(?P<name>[A-Za-z_]\w*)'"),
    re.compile(r"use of undeclared identifier '(?P<name>[A-Za-z_]\w*)'"),
    re.compile(r"unknown type name '(?P<name>[A-Za-z_]\w*)'"),
    re.compile(r"has no member named '(?P<name>[A-Za-z_]\w*)'"),
    re.compile(r"no member named '(?P<name>[A-Za-z_]\w*)'"),
)
_MEMBER = (
    re.compile(
        r"'(?:struct |union )?(?P<owner>[A-Za-z_]\w*)'(?: \{aka '[^']*'\})?"
        r" has no member named '(?P<name>[A-Za-z_]\w*)'"
    ),
    re.compile(
        r"no member named '(?P<name>[A-Za-z_]\w*)' in '(?:struct |union )?(?P<owner>[A-Za-z_]\w*)'"
    ),
)
_LINE_MARKER = re.compile(r'^# (?P<line>\d+) "(?P<file>(?:[^"\\]|\\.)*)"')
_DIRECTIVE = re.compile(r"^\s*#\s*(?P<kind>ifdef|ifndef|if|elif|else|endif)\b(?P<rest>.*)$")
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")


class MatrixError(ValueError):
    """The matrix or a fragment breaks a rule; the message names the file and what is wrong."""


# -- the matrix file -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Row:
    """One configuration: its allocated id, one word per axis, and what it is for."""

    id: str
    words: Mapping[str, str]
    description: str

    @property
    def name(self) -> str:
        """Return ``<id>_<toolchain>_<platform>_<features>_<target>_<library>``."""
        return "_".join([self.id, *(self.words[axis] for axis in AXES)])


@dataclass(frozen=True)
class Platform:
    """What a platform word means to a compiler: target or host, include folders, defines, flags."""

    name: str
    description: str = ""
    host: bool = False
    includes: tuple[Path, ...] = ()
    defines: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Exemption:
    """Lines no row compiles, explained: a file pattern, an identifier of the hiding directive."""

    file: str
    guard: str
    why: str

    def covers(self, file: str, guard: str) -> bool:
        """Return True when this explains a line of ``file`` hidden by the directive ``guard``.

        The guard is an identifier the hiding directive names; an empty guard explains every line
        of the matching files, a file no row reaches among them.
        """
        if not fnmatch.fnmatchcase(file, self.file):
            return False
        return not self.guard or self.guard in _IDENTIFIER.findall(guard)


@dataclass(frozen=True)
class Acceptance:
    """A finding kept by decision: a file pattern, a text of the message, the reason."""

    file: str
    text: str
    why: str

    def covers(self, file: str, text: str) -> bool:
        """Return True when this keeps a finding of ``file`` (root-relative) saying ``text``."""
        return fnmatch.fnmatchcase(file, self.file) and self.text in text


@dataclass(frozen=True)
class MatrixFile:
    """The rows, the platforms and the exemptions of one matrix file, and what holds for all rows.

    ``coverage`` maps an axis to the words whose rows are measured; an axis it does not name
    counts every word.
    """

    rows: tuple[Row, ...]
    platforms: Mapping[str, Platform]
    exemptions: tuple[Exemption, ...]
    defines: tuple[str, ...] = ()
    includes: tuple[Path, ...] = ()
    coverage: Mapping[str, frozenset[str]] = field(default_factory=dict)
    accepted: tuple[Acceptance, ...] = ()

    def measures(self, row: Row) -> bool:
        """Return True when ``row`` counts for coverage: every axis word the table allows."""
        return all(row.words[axis] in words for axis, words in self.coverage.items())


def _table(raw: object, where: str, keys: Sequence[str], required: Sequence[str]) -> dict[str, Any]:
    """Return ``raw`` as a table holding every required key and nothing outside ``keys``."""
    if not isinstance(raw, dict):
        msg = f"{where}: is not a table"
        raise MatrixError(msg)
    missing = [key for key in required if key not in raw]
    if missing:
        msg = f"{where}: missing {', '.join(missing)}"
        raise MatrixError(msg)
    unknown = [key for key in raw if key not in keys]
    if unknown:
        msg = f"{where}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(keys)}"
        raise MatrixError(msg)
    return raw


def _strings(raw: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    """Return ``raw[key]`` as a tuple of strings; an absent key is the empty tuple."""
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{where}: {key} is a list of strings"
        raise MatrixError(msg)
    return tuple(value)


def parse_row(raw: object, where: str) -> Row:
    """Return one ``[[row]]`` table as a :class:`Row`, every rule of a row checked."""
    table = _table(raw, where, _ROW_KEYS, _ROW_KEYS)
    row_id = str(table["id"])
    if not _ID.match(row_id):
        msg = f"{where}: id {row_id!r} is not 4 to 6 digits"
        raise MatrixError(msg)
    words = {axis: str(table[axis]) for axis in AXES}
    bad = [f"{axis}={word!r}" for axis, word in words.items() if not _WORD.match(word)]
    if bad:
        msg = f"{where}: a word is lowercase letters and digits joined by '-': {', '.join(bad)}"
        raise MatrixError(msg)
    description = str(table["description"]).strip()
    if not description:
        msg = f"{where}: description is empty"
        raise MatrixError(msg)
    return Row(row_id, words, description)


def parse_platform(name: str, raw: object, root: Path) -> Platform:
    """Return one ``[platform.<word>]`` table; its include folders are relative to ``root``."""
    where = f"platform.{name}"
    if not _WORD.match(name):
        msg = f"{where}: a word is lowercase letters and digits joined by '-'"
        raise MatrixError(msg)
    table = _table(raw, where, _PLATFORM_KEYS, ("description",))
    host = table.get("host", False)
    if not isinstance(host, bool):
        msg = f"{where}: host is true or false"
        raise MatrixError(msg)
    return Platform(
        name=name,
        description=str(table["description"]),
        host=host,
        includes=tuple(root / item for item in _strings(table, "includes", where)),
        defines=_strings(table, "defines", where),
        flags=_strings(table, "flags", where),
    )


def parse_matrix(text: str, root: Path, label: str = "matrix") -> MatrixFile:
    """Return the rows, platforms and exemptions of a matrix file's text."""
    data = tomllib.loads(text)
    keys = ("defines", "includes", "coverage", "platform", "exempt", "accept", "row")
    unknown = [key for key in data if key not in keys]
    if unknown:
        msg = f"{label}: unknown top-level key(s) {', '.join(unknown)}; allowed: {', '.join(keys)}"
        raise MatrixError(msg)
    rows = tuple(parse_row(raw, f"{label} row {n}") for n, raw in enumerate(data.get("row", []), 1))
    seen: set[str] = set()
    for row in rows:
        if row.id in seen:
            msg = f"{label}: id {row.id} is allocated twice"
            raise MatrixError(msg)
        seen.add(row.id)
    platforms = {
        name: parse_platform(name, raw, root) for name, raw in data.get("platform", {}).items()
    }
    exemptions = []
    for n, raw in enumerate(data.get("exempt", []), 1):
        table = _table(raw, f"{label} exempt {n}", _EXEMPT_KEYS, _EXEMPT_KEYS)
        exemptions.append(Exemption(str(table["file"]), str(table["guard"]), str(table["why"])))
    accepted = []
    for n, raw in enumerate(data.get("accept", []), 1):
        table = _table(raw, f"{label} accept {n}", _ACCEPT_KEYS, _ACCEPT_KEYS)
        accepted.append(Acceptance(str(table["file"]), str(table["text"]), str(table["why"])))
    coverage_raw = data.get("coverage", {})
    if not isinstance(coverage_raw, dict) or any(axis not in AXES for axis in coverage_raw):
        msg = f"{label}: coverage is a table of axes ({', '.join(AXES)}) to lists of words"
        raise MatrixError(msg)
    coverage = {
        axis: frozenset(_strings(coverage_raw, axis, f"{label} coverage")) for axis in coverage_raw
    }
    return MatrixFile(
        rows,
        platforms,
        tuple(exemptions),
        defines=_strings(data, "defines", label),
        includes=tuple(root / item for item in _strings(data, "includes", label)),
        coverage=coverage,
        accepted=tuple(accepted),
    )


def load_matrix(path: Path, root: Path) -> MatrixFile:
    """Read a matrix file; ``root`` is the folder its platform include folders are relative to."""
    return parse_matrix(path.read_text(encoding="ascii"), root, path.name)


def fragment_path(fragments: Path, axis: str, word: str) -> Path:
    """Return the fragment file of ``word`` on ``axis``: ``<fragments>/<axis>/<word>.h``."""
    return fragments / axis / f"{word}.h"


def missing_fragments(rows: Iterable[Row], fragments: Path) -> list[str]:
    """Return the fragment files the rows name and the repository does not hold, each once."""
    missing: dict[str, None] = {}
    for row in rows:
        for axis in AXES:
            path = fragment_path(fragments, axis, row.words[axis])
            if not path.is_file():
                missing[f"{axis}/{row.words[axis]}.h"] = None
    return list(missing)


def select(rows: Sequence[Row], only: Iterable[str]) -> list[Row]:
    """Return the rows whose id or name starts with one of ``only``; every row when none given."""
    wanted = tuple(only)
    if not wanted:
        return list(rows)
    return [r for r in rows if any(r.id.startswith(w) or r.name.startswith(w) for w in wanted)]


# -- the configuration header -------------------------------------------------------------------
@dataclass
class Define:
    """One ``#define`` line of the base header, active or commented out, with its value text."""

    name: str
    active: bool
    rest: str  # everything after the name: the value and the trailing comment, as written

    def render(self) -> str:
        """Return the line as it is written back."""
        return ("" if self.active else "//") + f"#define {self.name}{self.rest}"


def parse_config(text: str) -> list[tuple[int, Define]]:
    """Return every define line of a configuration header with its line index (0-based)."""
    found = []
    for index, line in enumerate(text.splitlines()):
        m = _DEFINE.match(line)
        if m:
            found.append((index, Define(m["name"], m["off"] is None, m["rest"].rstrip())))
    return found


Operation = tuple[str, str, str | None]
"""One fragment line: ``("define", name, value or None)`` or ``("undef", name, None)``."""


def parse_fragment(path: Path, _seen: frozenset[Path] = frozenset()) -> list[Operation]:
    """Return a fragment's operations in order, ``#include`` lines expanded where they stand.

    A fragment holds ``#define NAME [value]``, ``#undef NAME``, ``#include "other.h"`` (a
    fragment beside this one), ``//`` comments and blank lines; anything else, and an include that
    comes back to a fragment already being read, is an error naming the line.
    """
    resolved = path.resolve()
    if resolved in _seen:
        msg = f"{path}: includes itself"
        raise MatrixError(msg)
    operations: list[Operation] = []
    for number, raw in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if m := _FRAGMENT_DEFINE.match(line):
            operations.append(("define", m["name"], m["value"]))
        elif m := _FRAGMENT_UNDEF.match(line):
            operations.append(("undef", m["name"], None))
        elif m := _FRAGMENT_INCLUDE.match(line):
            operations.extend(parse_fragment(path.parent / m["file"], _seen | {resolved}))
        else:
            msg = f"{path}:{number}: a fragment line is #define, #undef, #include or a comment"
            raise MatrixError(msg)
    return operations


def synthesize(base: str, operations: Iterable[Operation], *, append: bool = False) -> str:
    """Return the base header with the operations applied, in order.

    A define activates the FIRST line of that name, so an example line left commented below it
    stays an example, and replaces the value when the fragment gives one, keeping the line's
    trailing comment; an undef comments every line of the name out. A name the base does not
    declare is an error - every switch is declared there - or, with ``append``, it is added in a
    block before the closing ``#endif`` of the include guard, which is what a product header needs
    that leaves its build switches to the command line.
    """
    lines = base.splitlines()
    defines = parse_config(base)
    appended: dict[str, str] = {}
    for op, name, value in operations:
        hits = [d for _, d in defines if d.name == name]
        if op == "undef":
            for d in hits:
                d.active = False
            appended.pop(name, None)
        elif hits:
            hits[0].active = True
            if value is not None:
                comment = hits[0].rest.partition("//")[2]
                hits[0].rest = f" {value}" + (f" //{comment}" if comment else "")
        elif append:
            appended[name] = f"#define {name}" + (f" {value}" if value is not None else "")
        else:
            msg = f"{name}: not declared by the base header, where every switch is declared"
            raise MatrixError(msg)
    for index, d in defines:
        lines[index] = d.render()
    if appended:
        endifs = [i for i, line in enumerate(lines) if line.startswith("#endif")]
        at = endifs[-1] if endifs else len(lines)
        lines[at:at] = ["// Matrix: switches the base header does not declare", *appended.values()]
    return "\n".join(lines) + "\n"


def row_operations(fragments: Path, row: Row) -> list[Operation]:
    """Return the operations of ``row``: its five fragments' in axis order."""
    operations: list[Operation] = []
    for axis in AXES:
        operations.extend(parse_fragment(fragment_path(fragments, axis, row.words[axis])))
    return operations


def row_config(base: Path, fragments: Path, row: Row, *, append: bool = False) -> str:
    """Return the synthesized header of ``row``: the base plus its five fragments in axis order."""
    operations = row_operations(fragments, row)
    return synthesize(base.read_text(encoding="utf-8"), operations, append=append)


def declared(text: str) -> set[str]:
    """Return the names a configuration header declares, active or commented out."""
    return {d.name for _, d in parse_config(text)}


# -- compilers -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Compiler:
    """A toolchain word on this machine: the executable and the flags it adds.

    ``target_flags`` apply on every platform that is not the host (clang's ``--target=`` and the
    target C library), ``host_flags`` on the host; ``diagnostics`` puts the compiler's messages in
    the one-line form (:data:`GCC_DIAGNOSTICS`, :data:`CLANG_DIAGNOSTICS`); ``env`` is the
    environment the compiler needs (the MSVC one, for clang on a Windows host), ``None`` = as is.
    """

    name: str
    exe: Path
    target_flags: tuple[str, ...] = ()
    host_flags: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    warnings: tuple[str, ...] = WARNINGS
    std: str = "gnu99"
    env: Mapping[str, str] | None = None

    def base_argv(self, platform: Platform) -> list[str]:
        """Return the compiler, the dialect and the flags of ``platform``, before any mode."""
        own = self.host_flags if platform.host else self.target_flags
        return [str(self.exe), f"-std={self.std}", *own, *platform.flags, *self.diagnostics]


def compile_argv(
    compiler: Compiler,
    platform: Platform,
    source: Path,
    includes: Iterable[Path],
    mode: str = "syntax",
    defines: Iterable[str] = (),
) -> list[str]:
    """Return the argv of one source: ``syntax`` checks, ``coverage`` preprocesses, ``deps`` lists.

    The include folders come first (the row's configuration, then the repository's own, then the
    ones every row shares), the platform's after them, so a fake can never shadow a header of the
    repository; ``defines`` (every row's) come before the platform's.
    """
    modes = {
        "syntax": ["-fsyntax-only", *compiler.warnings],
        "coverage": ["-E"],
        "deps": ["-E", "-M", "-MG"],
    }
    return [
        *compiler.base_argv(platform),
        *modes[mode],
        *[f"-D{define}" for define in (*defines, *platform.defines)],
        *[f"-I{folder}" for folder in includes],
        *[f"-I{folder}" for folder in platform.includes],
        str(source),
    ]


def run(
    argv: Sequence[str], env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one compiler command and return it; the caller reads the exit code and the streams."""
    return subprocess.run(  # noqa: S603 - the argv is built by this module from the caller's lists
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        errors="replace",
        env=None if env is None else dict(env),
    )


# -- diagnostics ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One compiler diagnostic: where, what kind, and the message."""

    file: str
    line: int
    column: int
    kind: str
    text: str

    def render(self) -> str:
        """Return the finding in the compiler's own ``file:line:col: kind: text`` form."""
        return f"{self.file}:{self.line}:{self.column}: {self.kind}: {self.text}"


def parse_diagnostics(output: str) -> list[Finding]:
    """Return every error, fatal error and warning of a compiler's output; notes are context."""
    findings = []
    for line in output.splitlines():
        m = _DIAGNOSTIC.match(line)
        if m:
            findings.append(
                Finding(m["file"], int(m["line"]), int(m["col"]), m["kind"], m["text"].rstrip())
            )
    return findings


def missing_header(finding: Finding) -> str | None:
    """Return the header a fatal 'file not found' finding names, else None."""
    m = _MISSING_HEADER.match(finding.text)
    if m is None:
        return None
    return m["clang"] or m["gcc"]


def undeclared(findings: Iterable[Finding]) -> list[str]:
    """Return the identifiers the findings call undeclared, unknown or missing, each once.

    A missing member is named with the structure it is missing from, ``Owner.member``: a member
    one of the repository's own structures lacks is then the repository's finding by the owner's
    prefix, and one a vendor's structure lacks the fake's.
    """
    names: dict[str, None] = {}
    for f in findings:
        member = next((m for pattern in _MEMBER if (m := pattern.search(f.text))), None)
        if member is not None:
            names[f"{member['owner']}.{member['name']}"] = None
            continue
        for pattern in _UNDECLARED:
            m = pattern.search(f.text)
            if m:
                names[m["name"]] = None
    return list(names)


def unresolved_headers(deps: str) -> list[str]:
    """Return the headers of a ``-M -MG`` rule that name no file: the ones the compiler lacks.

    ``-MG`` lists a header it cannot find exactly as the include wrote it, where every header it
    found is listed with the folder it was found in; a name that is not a file is therefore one the
    compiler would have stopped on.
    """
    body = deps.replace("\\\r\n", " ").replace("\\\n", " ")
    _, _, body = body.partition(": ")
    items = re.findall(r"(?:\\ |[^\s])+", body)
    names = [item.replace("\\ ", " ") for item in items]
    return [name for name in dict.fromkeys(names[1:]) if not Path(name).is_file()]


# -- coverage ------------------------------------------------------------------------------------
def strip_comments(text: str) -> str:
    """Return ``text`` with every comment blanked, newlines kept, literals left alone."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class Structure:
    """The conditional structure of one source file, as far as coverage needs it.

    ``owner[n]`` is the innermost branch holding line ``n`` (1-based; index 0 unused), -1 for a
    line outside every ``#if``; ``parent[b]`` and ``guard[b]`` are the enclosing branch and the
    directive of branch ``b``; ``code`` is the set of lines that carry code.
    """

    owner: tuple[int, ...]
    parent: tuple[int, ...]
    guard: tuple[str, ...]
    code: frozenset[int]


def structure(text: str) -> Structure:
    """Return the branches, their nesting and the code lines of a C source."""
    lines = strip_comments(text).splitlines()
    owner = [-1] * (len(lines) + 1)
    parent: list[int] = []
    guard: list[str] = []
    code: set[int] = set()
    groups: list[tuple[str, int]] = []  # (the group's opening directive, its current branch)
    current = -1
    continuing = False
    for number, raw in enumerate(lines, 1):
        body = raw.strip()
        directive = None if continuing else _DIRECTIVE.match(body)
        was_continuing, continuing = continuing, body.endswith("\\")
        if directive:
            kind = directive["kind"]
            text_of = f"#{kind}{directive['rest'].rstrip()}"
            if kind in ("if", "ifdef", "ifndef"):
                groups.append((text_of, len(parent)))
                parent.append(current)
                guard.append(text_of)
                current = len(parent) - 1
            elif groups and kind in ("elif", "else"):
                opening, branch = groups[-1]
                parent.append(parent[branch])
                guard.append(f"{text_of} (after {opening})")
                groups[-1] = (opening, len(parent) - 1)
                current = len(parent) - 1
            elif groups:  # endif
                _, branch = groups.pop()
                current = parent[branch]
            owner[number] = current
            continue
        owner[number] = current
        if body and not body.startswith("#") and not was_continuing:
            code.add(number)
    return Structure(tuple(owner), tuple(parent), tuple(guard), frozenset(code))


def _unescape(file: str) -> str:
    return re.sub(r"\\(.)", r"\1", file)


def emitted_lines(preprocessed: str) -> dict[str, set[int]]:
    """Return, per file named in the line markers, the lines the preprocessor output has text for.

    The markers (``# 12 "file"``) say which file and line the next output line comes from; a blank
    output line is a comment, a directive, a skipped line or the tail of a statement whose text
    came out on its first line, so only lines with text are recorded.
    """
    found: dict[str, set[int]] = {}
    current: set[int] | None = None
    number = 0
    for out in preprocessed.splitlines():
        m = _LINE_MARKER.match(out)
        if m:
            number = int(m["line"])
            current = found.setdefault(_unescape(m["file"]), set())
            continue
        if current is not None and out.strip():
            current.add(number)
        number += 1
    return found


def taken_branches(shape: Structure, emitted: Iterable[int]) -> set[int]:
    """Return the branches a set of emitted lines proves compiled, their parents included."""
    taken: set[int] = set()
    for line in emitted:
        branch = shape.owner[line] if 0 < line < len(shape.owner) else -1
        while branch != -1 and branch not in taken:
            taken.add(branch)
            branch = shape.parent[branch]
    return taken


@dataclass(frozen=True)
class Gap:
    """A run of code lines of one file that no row compiled, and the directive that hid them."""

    first: int
    last: int
    guard: str
    why: str = ""

    def as_json(self) -> list[object]:
        """Return ``[first, last, guard]``, with the exemption's reason when it has one."""
        return [self.first, self.last, self.guard, *([self.why] if self.why else [])]


UNREACHED = "no source of any row includes this file"
"""The guard of a file no row reached at all; an exemption with an empty guard covers it."""


def gaps(shape: Structure, taken: set[int], *, reached: bool) -> list[tuple[int, int, str]]:
    """Return the runs of code lines left uncovered, each with the directive that hid it.

    A code line is covered when the branch holding it was compiled, or it is outside every branch
    and its file was reached; a file no row reached is one run, hidden by :data:`UNREACHED`.
    """
    if not reached:
        return [(min(shape.code), max(shape.code), UNREACHED)] if shape.code else []
    runs: list[tuple[int, int, str]] = []
    for line in sorted(shape.code):
        branch = shape.owner[line]
        if branch == -1 or branch in taken:
            continue
        hidden_by = shape.guard[branch]
        if runs and runs[-1][2] == hidden_by and _adjacent(shape, runs[-1][1], line):
            runs[-1] = (runs[-1][0], line, hidden_by)
        else:
            runs.append((line, line, hidden_by))
    return runs


def _adjacent(shape: Structure, last: int, line: int) -> bool:
    """Return True when nothing but non-code lines lies between two code lines."""
    return not any(n in shape.code for n in range(last + 1, line))


# -- the run -------------------------------------------------------------------------------------
@dataclass
class RowResult:
    """What one row produced: its verdict, every finding, the fake work list, its coverage."""

    row: Row
    compiler: str
    status: str = PASS
    findings: list[Finding] = field(default_factory=list)
    missing_headers: list[str] = field(default_factory=list)
    undeclared: list[str] = field(default_factory=list)
    files: int = 0
    accepted: list[tuple[Finding, str]] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        """Return the row's record for ``report.json``."""
        return {
            "id": self.row.id,
            "name": self.row.name,
            "words": dict(self.row.words),
            "description": self.row.description,
            "compiler": self.compiler,
            "status": self.status,
            "files": self.files,
            "findings_total": len(self.findings),
            "findings": [f.render() for f in self.findings[:FINDINGS_KEPT]],
            "missing_headers": list(self.missing_headers),
            "undeclared": list(self.undeclared),
            "accepted_total": len(self.accepted),
            "accepted": [f"{f.render()} - {why}" for f, why in self.accepted[:FINDINGS_KEPT]],
        }


@dataclass(frozen=True)
class Matrix:
    """Everything a run needs: the repository's files and lists, and the words' meaning here.

    ``root`` is the repository root coverage is measured under; ``base`` the configuration header
    every row starts from (the library's template, or a product's own header), written into the
    row's configuration folder as ``config_name`` (its own name when empty); ``sources`` the
    files every row compiles; ``measured`` the files whose lines must be covered (the sources and
    the headers they reach); ``includes`` the repository's include folders, searched after the
    row's configuration folder; ``aliases`` headers written into that folder that include a file
    of the repository under another name (a product-supplied ``*_usr.h`` that is the library's
    template); ``own`` the prefixes of the repository's own identifiers - an undeclared one is a
    finding of the repository, not a fake to write; ``headers`` the repository's further
    configuration headers, by the name its sources include them (a product configures its own
    modules in headers of their own): each is written into the folder too, with the row's
    operations on the names it declares, and the base takes the rest.
    """

    root: Path
    base: Path
    fragments: Path
    matrix: MatrixFile
    sources: Sequence[Path]
    measured: Sequence[Path]
    includes: Sequence[Path]
    compilers: Mapping[str, Compiler]
    platforms: Mapping[str, Platform] = field(default_factory=dict)
    aliases: Mapping[str, Path] = field(default_factory=dict)
    own: tuple[str, ...] = ()
    append: bool = False
    heads: Mapping[str, str] = field(default_factory=dict)
    config_name: str = ""
    headers: Mapping[str, Path] = field(default_factory=dict)

    def platform(self, word: str) -> Platform:
        """Return the platform of a word: the caller's definition first, then the matrix file's."""
        if word in self.platforms:
            return self.platforms[word]
        return self.matrix.platforms[word]

    def check(self, rows: Iterable[Row]) -> None:
        """Raise MatrixError naming every word of ``rows`` this run cannot give a meaning."""
        rows = list(rows)
        problems = [f"fragment {name} missing" for name in missing_fragments(rows, self.fragments)]
        for row in rows:
            if row.words["toolchain"] not in self.compilers:
                problems.append(f"{row.name}: no compiler for {row.words['toolchain']!r}")
            if row.words["platform"] not in {**self.matrix.platforms, **self.platforms}:
                problems.append(f"{row.name}: no platform {row.words['platform']!r}")
        if problems:
            raise MatrixError("; ".join(dict.fromkeys(problems)))


def row_headers(matrix: Matrix, row: Row) -> dict[str, str]:
    """Return every configuration header of ``row`` by its name: the base's, then ``headers``.

    An operation goes to every header declaring its name; one no further header declares goes to
    the base, which declares it or, with ``append``, takes it in - else it is an error there.
    """
    operations = row_operations(matrix.fragments, row)
    texts = {name: path.read_text(encoding="utf-8") for name, path in matrix.headers.items()}
    names = {name: declared(text) for name, text in texts.items()}
    elsewhere = set().union(*names.values())
    base = matrix.base.read_text(encoding="utf-8")
    own = declared(base)
    kept = [op for op in operations if op[1] in own or op[1] not in elsewhere]
    headers = {matrix.config_name or matrix.base.name: synthesize(base, kept, append=matrix.append)}
    for name, text in texts.items():
        headers[name] = synthesize(text, [op for op in operations if op[1] in names[name]])
    return headers


def write_config(matrix: Matrix, row: Row, out: Path) -> Path:
    """Write the row's configuration folder under ``out`` and return it."""
    folder = out / "config"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in row_headers(matrix, row).items():
        (folder / name).write_text(text, encoding="utf-8", newline="\n")
    for name, target in matrix.aliases.items():
        (folder / name).write_text(
            f'#include "{target.resolve().as_posix()}"\n', encoding="ascii", newline="\n"
        )
    return folder


@dataclass(frozen=True)
class _Job:
    row: Row
    source: Path
    argv: tuple[str, ...]
    coverage: tuple[str, ...] | None
    deps: tuple[str, ...]
    env: Mapping[str, str] | None


def _jobs(matrix: Matrix, rows: Sequence[Row], out: Path) -> list[_Job]:
    jobs: list[_Job] = []
    for row in rows:
        compiler = matrix.compilers[row.words["toolchain"]]
        platform = matrix.platform(row.words["platform"])
        includes = [
            write_config(matrix, row, out / row.name),
            *matrix.includes,
            *matrix.matrix.includes,
        ]
        defines = matrix.matrix.defines
        measured = matrix.matrix.measures(row)
        jobs.extend(
            _Job(
                row,
                source,
                tuple(compile_argv(compiler, platform, source, includes, "syntax", defines)),
                tuple(compile_argv(compiler, platform, source, includes, "coverage", defines))
                if measured
                else None,
                tuple(compile_argv(compiler, platform, source, includes, "deps", defines)),
                compiler.env,
            )
            for source in matrix.sources
        )
    return jobs


def _execute(
    job: _Job, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> tuple[list[Finding], list[str], dict[str, set[int]]]:
    """Compile one source of one row: its findings, the headers it lacks, its emitted lines."""
    done = runner(job.argv, job.env)
    findings = parse_diagnostics(done.stderr + done.stdout)
    if done.returncode != 0 and not findings:
        text = f"exit {done.returncode} without a diagnostic: {(done.stderr or done.stdout)[:200]}"
        findings = [Finding(str(job.source), 0, 0, "error", text.strip())]
    lacking: list[str] = []
    if any(missing_header(f) for f in findings):
        lacking = unresolved_headers(runner(job.deps, job.env).stdout)
    emitted = {} if job.coverage is None else emitted_lines(runner(job.coverage, job.env).stdout)
    return findings, lacking, emitted


def _key(root: Path, file: str, cache: dict[str, str | None]) -> str | None:
    """Return a marker's file as a path relative to ``root``, None outside it; resolved once."""
    if file not in cache:
        try:
            cache[file] = Path(file).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            cache[file] = None
    return cache[file]


def _status(result: RowResult, own: tuple[str, ...]) -> str:
    """Return NO SHIM when a fake is missing, FAIL on any other finding, else PASS."""
    lacking = [name for name in result.undeclared if not (own and name.startswith(own))]
    if result.missing_headers or lacking:
        return NO_SHIM
    return FAIL if result.findings else PASS


def coverage_report(
    matrix: Matrix, emitted: Mapping[str, set[int]]
) -> tuple[dict[str, int], dict[str, list[Gap]], dict[str, list[Gap]]]:
    """Return the totals, the uncovered runs and the exempted runs of every measured file."""
    totals = {"files": 0, "code_lines": 0, "covered": 0, "exempt": 0, "uncovered": 0}
    uncovered: dict[str, list[Gap]] = {}
    exempted: dict[str, list[Gap]] = {}
    root = matrix.root.resolve()
    for path in matrix.measured:
        key = path.resolve().relative_to(root).as_posix()
        shape = structure(path.read_text(encoding="utf-8", errors="replace"))
        taken = taken_branches(shape, emitted.get(key, set()))
        runs = gaps(shape, taken, reached=key in emitted)
        totals["files"] += 1
        totals["code_lines"] += len(shape.code)
        for first, last, guard in runs:
            count = sum(1 for n in shape.code if first <= n <= last)
            reason = next((e.why for e in matrix.matrix.exemptions if e.covers(key, guard)), "")
            if reason:
                exempted.setdefault(key, []).append(Gap(first, last, guard, reason))
                totals["exempt"] += count
            else:
                uncovered.setdefault(key, []).append(Gap(first, last, guard))
                totals["uncovered"] += count
    totals["covered"] = totals["code_lines"] - totals["exempt"] - totals["uncovered"]
    return totals, uncovered, exempted


def run_matrix(
    matrix: Matrix,
    out: Path,
    *,
    only: Sequence[str] = (),
    runner: Callable[..., subprocess.CompletedProcess[str]] = run,
    workers: int | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the selected rows, write ``report.json`` and ``report.md`` under ``out``, return it.

    Every source of every row is one job in one pool, so the slowest row does not idle the
    machine; the rows' configuration folders are written first, under ``out/<row name>/config``,
    and each row's findings land in ``out/<row name>/findings.txt``.
    """
    rows = select(matrix.matrix.rows, only)
    if not rows:
        msg = f"no row matches {list(only)}"
        raise MatrixError(msg)
    matrix.check(rows)
    jobs = _jobs(matrix, rows, out)
    results = {
        row.name: RowResult(row, row.words["toolchain"], files=len(matrix.sources)) for row in rows
    }
    emitted: dict[str, set[int]] = {}
    lacking: dict[str, dict[str, None]] = {row.name: {} for row in rows}
    cache: dict[str, str | None] = {}
    root = matrix.root.resolve()
    accepts = matrix.matrix.accepted
    with ThreadPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        for job, (findings, missing, lines) in zip(
            jobs, pool.map(lambda j: _execute(j, runner), jobs), strict=True
        ):
            for finding in findings:
                where = _key(root, finding.file, cache) or ""
                why = next((a.why for a in accepts if a.covers(where, finding.text)), None)
                if why is None:
                    results[job.row.name].findings.append(finding)
                else:
                    results[job.row.name].accepted.append((finding, why))
            lacking[job.row.name].update(dict.fromkeys(missing))
            for file, numbers in lines.items():
                key = _key(root, file, cache)
                if key is not None:
                    emitted.setdefault(key, set()).update(numbers)
    for row in rows:
        result = results[row.name]
        named = [h for f in result.findings if (h := missing_header(f))]
        result.missing_headers = list(dict.fromkeys([*named, *lacking[row.name]]))
        result.undeclared = undeclared(result.findings)
        result.status = _status(result, matrix.own)
        (out / row.name / "findings.txt").write_text(
            "".join(f"{f.render()}\n" for f in result.findings), encoding="utf-8", newline="\n"
        )
        if log is not None:
            log(f"{result.status:7} {row.name} ({len(result.findings)} finding(s))")
    totals, uncovered, exempted = coverage_report(matrix, emitted)
    report: dict[str, Any] = {
        "source": {
            "kind": "configs",
            "schema": SCHEMA,
            "captured": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": "alx.verify.configs",
            "arguments": {"only": list(only), "rows": len(rows), "of": len(matrix.matrix.rows)},
            "base": matrix.base.name,
            "sources": len(matrix.sources),
            "compilers": {
                name: _version(c, runner) for name, c in sorted(matrix.compilers.items())
            },
            "heads": dict(matrix.heads),
        },
        "rows": [results[row.name].as_json() for row in rows],
        "coverage": totals,
        "uncovered": {k: [g.as_json() for g in v] for k, v in uncovered.items()},
        "exempted": {k: [g.as_json() for g in v] for k, v in exempted.items()},
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8", newline="\n")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return report


def _version(compiler: Compiler, runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    """Return the first line a compiler prints for ``--version``."""
    lines = runner([str(compiler.exe), "--version"], compiler.env).stdout.splitlines()
    return lines[0] if lines else str(compiler.exe)


def render_markdown(report: Mapping[str, Any]) -> str:
    """Return a report for a person: the rows, the fake work lists, the lines no row compiled."""
    totals = report["coverage"]
    lines = [
        "# Configuration matrix",
        "",
        f"{len(report['rows'])} of {report['source']['arguments']['of']} rows, "
        f"{report['source']['sources']} sources each. Code lines: {totals['code_lines']}, covered "
        f"{totals['covered']}, exempt {totals['exempt']}, uncovered {totals['uncovered']}.",
        "",
        "| row | status | findings | missing headers | undeclared |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| `{r['name']}` | {r['status']} | {r['findings_total']} | "
        f"{len(r['missing_headers'])} | {len(r['undeclared'])} |"
        for r in report["rows"]
    ]
    for r in report["rows"]:
        if r["status"] == PASS:
            continue
        lines += ["", f"## {r['name']}", "", r["description"], ""]
        if r["missing_headers"]:
            lines += ["Missing headers: " + ", ".join(f"`{h}`" for h in r["missing_headers"]), ""]
        if r["undeclared"]:
            lines += ["Undeclared: " + ", ".join(f"`{n}`" for n in r["undeclared"]), ""]
        shown = r["findings"][:40]
        lines += ["```", *shown, *(["..."] if r["findings_total"] > len(shown) else []), "```"]
    lines += ["", "## Lines no row compiled", ""]
    lines += _gap_lines(report["uncovered"]) or ["none"]
    lines += ["", "## Lines explained by an exemption", ""]
    lines += _gap_lines(report["exempted"]) or ["none"]
    kept = dict.fromkeys(line for r in report["rows"] for line in r["accepted"])
    lines += ["", "## Findings kept by decision", ""]
    lines += [f"- {line}" for line in kept] or ["none"]
    return "\n".join(lines) + "\n"


def _gap_lines(table: Mapping[str, list[list[Any]]]) -> list[str]:
    out = []
    for file, runs in table.items():
        out.append(f"- `{file}`")
        for run_ in runs:
            first, last, guard = run_[0], run_[1], run_[2]
            span = str(first) if first == last else f"{first}-{last}"
            why = f" - {run_[3]}" if run_[3:] else ""
            out.append(f"  - {span}: `{guard}`{why}")
    return out
