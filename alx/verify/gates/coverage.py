# SPDX-License-Identifier: MIT
"""Coverage gate: each file of a coverage report meets the minimum in every gated metric.

Four report formats, one gate for every language of the pipeline:

* cobertura XML - what coverage.py (``--cov-report=xml``), lcov-cobertura (the C library's llvm-cov
  lane) and coverlet (C#) all write: lines and branches
* llvm-cov export JSON (``llvm-cov export -summary-only``): lines, branches, regions and functions,
  the C library's native report
* coverage.py JSON (``--cov-report=json``): lines and branches with the missing-line detail
* an lcov tracefile (``.info``, ``llvm-cov export -format=lcov``): lines, branches and functions,
  computed here from its records, so that a source line can be exempted

A line no test can reach - the ``default`` of a switch over an enumeration, the ``else`` after an
exhaustive chain - carries one of lcov's exclusion markers (geninfo(1)) in a comment, followed by a
colon and the reason, which the gate lists::

    default: return 0;        // LCOV_EXCL_LINE: the switch is over the enumeration
    else if (a && b)          // LCOV_EXCL_BR_LINE: the chain before leaves no other case

``LCOV_EXCL_LINE`` takes the line out of lines, branches and functions, ``LCOV_EXCL_BR_LINE`` out
of branches only; ``LCOV_EXCL_START``/``LCOV_EXCL_STOP`` and ``LCOV_EXCL_BR_START``/``_BR_STOP``
mark a section (the start line in it, the stop line not). A marker without a reason fails, and
so does a stale one: an excluded line the run reached, or a branch-excluded line whose every
branch was taken - the exemption's reason no longer holds, as a strict xfail that passes.

The same exemptions can live outside the sources, in a TOML file named by ``--exclusions``, so
that a repository keeps its sources free of coverage comments (lcov's ``--omit-lines`` and
``--erase-functions`` do the same by option). One ``[[exclude]]`` table per exemption::

    [[exclude]]
    file = "alxParamItem.c"  # the report's path, exact or by its trailing components
    function = "AlxParamItem_SetVal"  # optional: the lines of this function alone are searched
    kind = "block"  # lines | branches | block | function
    match = "^.*else$"  # a regex over the line's text; a function's name for kind function
    nth = 2  # optional: the nth matching line of the scope, when there are several
    reason = "the thirteen data types above are the enumeration"

``lines`` takes every matching line out of lines, branches and functions; ``branches`` takes it out
of branches alone; ``block`` takes the block the matching line opens - the braces after it, or the
statements up to its ``break`` or ``return`` for a label, the opening line alone when it holds
one - out of every metric and the opening line out of branches; ``function`` takes a function's
body, its definition line to the closing brace at column 0. Each is listed under its file as the
markers are, and each fails when stale: a match the source no longer holds, a match that is
several lines without ``nth``, a function the report does not carry, an excluded line or function
the run reached, a branch exclusion whose every branch was taken.

A line that is a closing brace alone (``}``, with or without a comment) is no line: llvm-cov
charges the brace that closes a block after a ``return`` or a ``break`` with the region nothing
reaches, and a brace executes nothing. It is left out when the source can be read.

Every metric is gated on its own: a combined percentage would let missing branches hide behind
covered lines. A metric with nothing to cover (no branches in the file) counts as 100 %. Usage::

    python -m alx.verify.gates.coverage build/coverage/coverage.xml --min 100 [--out f] [file ...]
    python -m alx.verify.gates.coverage summary.json foo.c
        --metrics lines,branches,regions,functions
    python -m alx.verify.gates.coverage lcov.info --metrics lines,branches,functions
        --exclusions Test/config/coverage.toml

Without file arguments every file in the report is gated; a file argument matches the report's path
exactly or by its trailing components (``alxFoo.c`` matches ``C:/repo/alxFoo.c``). Exit code 0 =
PASS, 1 = FAIL. ``--pending alxFoo.c`` (repeatable) names a file known to be under the minimum: it
is reported and does not fail until it reaches the minimum, when it has to leave the list.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

METRICS = ("lines", "branches", "regions", "functions")
DEFAULT_METRICS = ("lines", "branches")


@dataclass(frozen=True)
class FileCoverage:
    """Per-file figures a report yields, normalized across formats (None = the format lacks it)."""

    name: str
    lines_pct: float
    branches_pct: float
    missing_lines: tuple[int, ...] = ()
    partial_branches: int = 0
    regions_pct: float | None = None
    functions_pct: float | None = None
    exempt: tuple[str, ...] = ()  # "line N: MARKER: reason", every marker the file carries
    faults: tuple[str, ...] = ()  # a marker without a reason, or one the run reached

    def metric(self, name: str) -> float | None:
        """Return the percentage of ``name`` (one of METRICS), None when the report lacks it."""
        value: float | None = getattr(self, f"{name}_pct")
        return value


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * covered / total


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def from_coverage_json(report: dict[str, Any]) -> list[FileCoverage]:
    """Read a coverage.py JSON report (``coverage json`` / ``--cov-report=json``)."""
    files = []
    for name, data in report["files"].items():
        summary = data["summary"]
        files.append(
            FileCoverage(
                _norm(name),
                _percent(summary["covered_lines"], summary["num_statements"]),
                _percent(summary["covered_branches"], summary["num_branches"]),
                tuple(int(n) for n in data.get("missing_lines", [])),
                int(summary.get("num_partial_branches", 0)),
            )
        )
    return files


def from_llvm(report: dict[str, Any]) -> list[FileCoverage]:
    """Read an llvm-cov export (``-summary-only``): lines, branches, regions and functions."""
    files = []
    for entry in report["data"][0]["files"]:
        summary = entry["summary"]

        def pct(metric: str, summary: dict[str, Any] = summary) -> float:
            block = summary.get(metric, {})
            return _percent(int(block.get("covered", 0)), int(block.get("count", 0)))

        files.append(
            FileCoverage(
                _norm(entry["filename"]),
                pct("lines"),
                pct("branches"),
                regions_pct=pct("regions"),
                functions_pct=pct("functions"),
            )
        )
    return files


def from_cobertura(root: ET.Element) -> list[FileCoverage]:
    """Read a cobertura XML tree: one entry per ``<class filename=...>`` with its rates."""
    files = []
    for cls in root.iter("class"):
        name = _norm(cls.get("filename") or cls.get("name") or "?")
        lines = list(cls.iter("line"))
        missing = tuple(int(line.get("number", "0")) for line in lines if line.get("hits") == "0")
        partial = sum(
            1
            for line in lines
            if line.get("branch") == "true"
            and not line.get("condition-coverage", "").startswith("100%")
        )
        files.append(
            FileCoverage(
                name,
                100.0 * float(cls.get("line-rate", "0")),
                100.0 * float(cls.get("branch-rate", "1")),
                missing,
                partial,
            )
        )
    return files


_BRACE_ONLY = re.compile(r"^\s*\}\s*;?\s*(?://.*|/\*.*\*/\s*)?$")
_MARKER = re.compile(
    r"(LCOV_EXCL_BR_START|LCOV_EXCL_BR_STOP|LCOV_EXCL_BR_LINE|LCOV_EXCL_START|LCOV_EXCL_STOP|"
    r"LCOV_EXCL_LINE)\b\s*(?::\s*(?P<reason>\S.*?))?\s*(?:\*/)?\s*$"
)


def exclusions(source: str) -> tuple[set[int], set[int], list[str], list[str]]:
    """Return (lines excluded, lines excluded from branches, the markers listed, the faults).

    The markers are lcov's (geninfo(1)); each needs ``: reason`` after it. A section's start line
    is in the section and its stop line is not, as geninfo has it. A start without a stop, a stop
    without a start, and a marker without a reason are faults.
    """
    excluded: set[int] = set()
    branches: set[int] = set()
    listed: list[str] = []
    faults: list[str] = []
    open_line: dict[str, int | None] = {"": None, "BR_": None}
    for number, text in enumerate(source.split("\n"), start=1):
        if _BRACE_ONLY.match(text):
            excluded.add(number)
        m = _MARKER.search(text)
        if not m:
            for kind, start in open_line.items():
                if start is not None:
                    (branches if kind else excluded).add(number)
            continue
        marker, reason = m.group(1), m.group("reason")
        if not reason:
            faults.append(f"line {number}: {marker} without a reason")
            reason = "?"
        listed.append(f"line {number}: {marker}: {reason}")
        kind = "BR_" if "_BR_" in marker else ""
        target = branches if kind else excluded
        if marker.endswith("_LINE"):
            target.add(number)
        elif marker.endswith("_START"):
            if open_line[kind] is not None:
                faults.append(
                    f"line {number}: {marker} inside a section open since line {open_line[kind]}"
                )
            open_line[kind] = number
            target.add(number)
        elif open_line[kind] is None:
            faults.append(f"line {number}: {marker} without a start")
        else:
            open_line[kind] = None
        for other, start in open_line.items():
            if start is not None and other != kind:
                (branches if other else excluded).add(number)
    for kind, start in open_line.items():
        if start is not None:
            faults.append(f"line {start}: LCOV_EXCL_{kind}START without a stop")
    return excluded, branches, listed, faults


def _marked(source: str) -> set[int]:
    """Return the lines a marker excludes on purpose: a reached one is a fault, a brace is not."""
    excluded: set[int] = set()
    open_at: int | None = None
    for number, text in enumerate(source.split("\n"), start=1):
        m = _MARKER.search(text)
        marker = m.group(1) if m else ""
        if marker == "LCOV_EXCL_LINE":
            excluded.add(number)
        elif marker == "LCOV_EXCL_START":
            open_at = number
        elif marker == "LCOV_EXCL_STOP":
            open_at = None
        if open_at is not None:
            excluded.add(number)
    return excluded


KINDS = ("lines", "branches", "block", "function")
_FUNCTION_DEF = re.compile(r"^[A-Za-z_][^\t;#/(]*\b(?P<name>[A-Za-z_]\w*)\s*(\(|$)")
_ENDS_BLOCK = re.compile(r"\bbreak\s*;|\breturn\b")


def load_exclusions(path: str | Path) -> list[dict[str, Any]]:
    """Read the ``[[exclude]]`` tables of a TOML file; each needs file, kind, match and reason."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    entries = data.get("exclude", [])
    for entry in entries:
        missing = [key for key in ("file", "kind", "match", "reason") if not entry.get(key)]
        if missing:
            raise ValueError(f"exclusion {entry}: missing {', '.join(missing)}")
        if entry["kind"] not in KINDS:
            raise ValueError(f"exclusion {entry}: unknown kind, from {', '.join(KINDS)}")
    return list(entries)


def _function_range(lines: list[str], name: str) -> tuple[int, int] | None:
    """Return the function's (first, last) lines, 1-based: definition to the ``}`` at column 0."""
    for index, text in enumerate(lines):
        m = _FUNCTION_DEF.match(text)
        if m is None or m.group("name") != name:
            continue
        for end in range(index + 1, len(lines)):
            if lines[end] == "}":
                return index + 1, end + 1
        return index + 1, len(lines)
    return None


def _block_range(lines: list[str], opener: int) -> tuple[int, int]:
    """Return the 1-based lines the block opened at line ``opener`` spans, the opener excluded.

    The braces after the opener, or - for a label or a condition without braces - the statements up
    to the first ``break`` or ``return``: the opener itself when it holds one.
    """
    if _ENDS_BLOCK.search(lines[opener - 1]):
        return opener, opener
    start = opener  # 0-based index of the line after the opener
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start].strip().startswith("{"):
        depth = 0
        for end in range(start, len(lines)):
            depth += lines[end].count("{") - lines[end].count("}")
            if depth <= 0:
                return opener + 1, end + 1
        return opener + 1, len(lines)
    for end in range(start, len(lines)):
        if _ENDS_BLOCK.search(lines[end]):
            return opener + 1, end + 1
    return opener + 1, len(lines)


def configured(
    source: str, entries: Iterable[dict[str, Any]], rec: dict[str, Any]
) -> tuple[set[int], set[int], list[str], list[str]]:
    """Apply a file's ``[[exclude]]`` entries: (lines excluded, branch-only lines, listed, faults).

    ``rec`` is the file's lcov record (``da``, ``br``, ``fn``, ``fnda``), read for the stale checks.
    """
    lines = source.split("\n")
    excluded: set[int] = set()
    branches: set[int] = set()
    listed: list[str] = []
    faults: list[str] = []
    for entry in entries:
        kind, match, reason = entry["kind"], str(entry["match"]), entry["reason"]
        scope = f" in {entry['function']}" if entry.get("function") else ""
        label = f"{kind} {match!r}{scope}"
        first, last = 1, len(lines)
        if entry.get("function"):
            span = _function_range(lines, str(entry["function"]))
            if span is None:
                faults.append(f"{label}: no function {entry['function']} in the source")
                continue
            first, last = span
        if kind == "function":
            span = _function_range(lines, match)
            if span is None or match not in rec["fn"]:
                faults.append(f"{label}: no function {match} in the source and the report")
                continue
            if rec["fnda"].get(match, 0) > 0:
                faults.append(f"{label}: excluded, and the run reached it")
            excluded.update(range(span[0], span[1] + 1))
            listed.append(f"{label} (lines {span[0]}-{span[1]}): {reason}")
            continue
        pattern = re.compile(match)
        hits = [n for n in range(first, last + 1) if pattern.search(lines[n - 1])]
        nth = entry.get("nth")
        if not hits:
            faults.append(f"{label}: matches nothing")
            continue
        if nth is not None:
            if nth > len(hits):
                faults.append(f"{label}: nth {nth}, {len(hits)} match(es)")
                continue
            hits = [hits[nth - 1]]
        elif len(hits) > 1 and kind == "block":
            faults.append(f"{label}: {len(hits)} matches, give nth")
            continue
        for n in hits:
            if kind == "lines":
                excluded.add(n)
                if rec["da"].get(n, 0) > 0:
                    faults.append(f"{label} (line {n}): excluded, and the run reached it")
                listed.append(f"{label} (line {n}): {reason}")
            elif kind == "branches":
                branches.add(n)
                if n in rec["br"] and all(t > 0 for t in rec["br"][n]):
                    faults.append(
                        f"{label} (line {n}): excluded from branches, and the run took every branch"
                    )
                listed.append(f"{label} (line {n}): {reason}")
            else:
                body_first, body_last = _block_range(lines, n)
                branches.add(n)
                if n in rec["br"] and all(t > 0 for t in rec["br"][n]):
                    faults.append(
                        f"{label} (line {n}): excluded from branches, and the run took every branch"
                    )
                body = range(body_first, body_last + 1)
                excluded.update(body)
                if any(
                    rec["da"].get(k, 0) > 0 and not _BRACE_ONLY.match(lines[k - 1]) for k in body
                ):
                    faults.append(
                        f"{label} (lines {body_first}-{body_last}): excluded, "
                        "and the run reached it"
                    )
                listed.append(f"{label} (lines {n}, {body_first}-{body_last}): {reason}")
    return excluded, branches, listed, faults


def from_lcov(
    text: str,
    read_source: Callable[[str], str | None] | None = None,
    config: Iterable[dict[str, Any]] = (),
) -> list[FileCoverage]:
    """Read an lcov tracefile: lines, branches and functions per file, the exemptions honoured.

    ``read_source(path)`` returns a source file's text, or None when it cannot be read (then nothing
    is excluded); by default the file named by the record is read. ``config`` holds the
    ``[[exclude]]`` entries of the TOML file, applied to the files they name.
    """
    if read_source is None:

        def read_source(path: str) -> str | None:
            try:
                return Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

    files: list[FileCoverage] = []
    records: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            cur = {"name": line[3:], "da": {}, "br": defaultdict(list), "fn": {}, "fnda": {}}
            records.append(cur)
        elif cur is None or line == "end_of_record":
            cur = None if line == "end_of_record" else cur
        elif line.startswith("DA:"):
            number, hits = line[3:].split(",")[:2]
            cur["da"][int(number)] = int(hits)
        elif line.startswith("BRDA:"):
            number, _block, _branch, taken = line[5:].split(",")
            cur["br"][int(number)].append(0 if taken == "-" else int(taken))
        elif line.startswith("FN:"):
            number, name = line[3:].split(",", 1)
            cur["fn"][name] = int(number)
        elif line.startswith("FNDA:"):
            hits, name = line[5:].split(",", 1)
            cur["fnda"][name] = int(hits)
    entries = list(config)
    for rec in records:
        source = read_source(rec["name"])
        excluded, br_excluded, listed, faults = (
            exclusions(source) if source is not None else (set(), set(), [], [])
        )
        marked = _marked(source) if source is not None else set()
        faults += [
            f"line {n}: excluded, and the run reached it"
            for n in sorted(excluded)
            if rec["da"].get(n, 0) > 0 and n in marked
        ]
        faults += [
            f"line {n}: excluded from branches, and the run took every branch"
            for n in sorted(br_excluded)
            if n in rec["br"] and all(t > 0 for t in rec["br"][n])
        ]
        mine = [e for e in entries if _matches(_norm(rec["name"]), _norm(str(e["file"])))]
        if mine and source is not None:
            c_excluded, c_branches, c_listed, c_faults = configured(source, mine, rec)
            excluded |= c_excluded
            br_excluded |= c_branches
            listed += c_listed
            faults += c_faults
        elif mine:
            faults.append("exclusions configured, and the source cannot be read")
        da = {n: h for n, h in rec["da"].items() if n not in excluded}
        br = {n: t for n, t in rec["br"].items() if n not in excluded and n not in br_excluded}
        fn = {name: n for name, n in rec["fn"].items() if n not in excluded}
        branches_taken = sum(1 for t in br.values() for x in t if x > 0)
        branches_total = sum(len(t) for t in br.values())
        files.append(
            FileCoverage(
                _norm(rec["name"]),
                _percent(sum(1 for h in da.values() if h > 0), len(da)),
                _percent(branches_taken, branches_total),
                tuple(n for n in sorted(da) if da[n] == 0),
                sum(1 for t in br.values() if any(x == 0 for x in t) and any(x > 0 for x in t)),
                functions_pct=_percent(
                    sum(1 for name in fn if rec["fnda"].get(name, 0) > 0), len(fn)
                ),
                exempt=tuple(listed),
                faults=tuple(faults),
            )
        )
    return files


def load(path: str | Path, exclusions: str | Path | None = None) -> list[FileCoverage]:
    """Load a report by its suffix: .xml cobertura, .info lcov, .json llvm-cov or coverage.py.

    ``exclusions`` names the TOML file of ``[[exclude]]`` entries; an lcov tracefile honours it.
    """
    path = Path(path)
    if path.suffix.lower() == ".info":
        entries = load_exclusions(exclusions) if exclusions else []
        return from_lcov(path.read_text(encoding="utf-8", errors="replace"), config=entries)
    if path.suffix.lower() == ".xml":
        # the report is written by our own coverage tool, not untrusted input
        return from_cobertura(ET.parse(path).getroot())  # noqa: S314
    report = json.loads(path.read_text(encoding="utf-8"))
    if str(report.get("type", "")).startswith("llvm.coverage"):
        return from_llvm(report)
    return from_coverage_json(report)


def _matches(name: str, wanted: str) -> bool:
    return name == wanted or name.endswith("/" + wanted)


def evaluate(
    files: Iterable[FileCoverage],
    minimum: float,
    wanted: Iterable[str] = (),
    metrics: Iterable[str] = DEFAULT_METRICS,
    pending: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """Return ``(rows, failures)``: a row per gated file, a failure per metric under the minimum.

    ``wanted`` narrows the gate to those files (exact path or trailing components); a wanted file
    absent from the report is a failure. ``metrics`` names what is gated; a gated metric the report
    does not carry is a failure too. A ``pending`` file (matched the same way) is reported under
    the minimum without failing, the repository's known queue; it fails once it reaches the
    minimum on every gated metric, so the queue shrinks by deletion, and a pending name no file of
    the report carries fails as stale. A file's exclusion markers are listed under its row, and
    its faults - a marker without a reason, a stale one - are failures.
    """
    want = [_norm(f) for f in wanted]
    waiting = [_norm(f) for f in pending]
    gated = tuple(metrics)
    unknown = [m for m in gated if m not in METRICS]
    if unknown:
        raise ValueError(f"unknown metric(s) {unknown}; known: {', '.join(METRICS)}")
    rows: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()
    still: set[str] = set()
    for fc in sorted(files, key=lambda f: f.name):
        held = [p for p in waiting if _matches(fc.name, p)]
        still.update(held)
        hits = [w for w in want if _matches(fc.name, w)]
        if want and not hits:
            continue
        seen.update(hits)
        cells = [f"{m} {value:6.1f}%" for m in METRICS if (value := fc.metric(m)) is not None]
        missing = ",".join(str(n) for n in fc.missing_lines)
        rows.append(
            f"{fc.name:<40} "
            + "  ".join(cells)
            + (f"  missing {missing}" if missing else "")
            + (f"  partial branches {fc.partial_branches}" if fc.partial_branches else "")
            + ("  (pending)" if held else "")
        )
        rows += [f"  exempt {e}" for e in fc.exempt]
        failures += [f"{fc.name}: {fault}" for fault in fc.faults]
        short = []
        for m in gated:
            value = fc.metric(m)
            if value is None:
                failures.append(f"{fc.name}: {m} not in the report")
            elif value < minimum:
                short.append(f"{fc.name}: {m} {value:.1f}% < {minimum:g}%")
        if not held:
            failures += short
        elif not short:
            failures.append(f"{fc.name}: pending, and at the minimum - take it off the list")
    failures.extend(f"{name}: not in the report" for name in want if name not in seen)
    failures.extend(
        f"{name}: pending, and not in the report" for name in waiting if name not in still
    )
    return rows, failures


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(
        prog="python -m alx.verify.gates.coverage", description=__doc__
    )
    parser.add_argument(
        "report", help="cobertura .xml, lcov .info, llvm-cov export .json or coverage.py .json"
    )
    parser.add_argument(
        "--min", type=float, default=100.0, help="minimum percent for every gated metric"
    )
    parser.add_argument(
        "--metrics",
        default=",".join(DEFAULT_METRICS),
        help=f"comma list of gated metrics, from {', '.join(METRICS)} (default lines,branches)",
    )
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument(
        "--pending", action="append", default=[], help="a file known to be under; repeatable"
    )
    parser.add_argument(
        "--exclusions", help="a TOML file of [[exclude]] tables, the lines no test can reach"
    )
    parser.add_argument(
        "files", nargs="*", help="files to gate (default: every file in the report)"
    )
    args = parser.parse_intermixed_args(argv)  # files may follow the options
    metrics = tuple(m.strip() for m in args.metrics.split(",") if m.strip())
    rows, failures = evaluate(
        load(args.report, args.exclusions), args.min, args.files, metrics, args.pending
    )
    verdict = "FAIL" if failures else "PASS"
    lines = [
        f"COVERAGE GATE: {verdict} (min {args.min:g}% on {', '.join(metrics)}, {len(rows)} files)"
    ]
    lines += rows
    lines += [f"  {f}" for f in failures]
    text = "\n".join(lines) + "\n"
    sys.stdout.write(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="ascii")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
