# SPDX-License-Identifier: MIT
r"""Static analysers over a configuration matrix: clang-tidy and cppcheck, row by row (D33).

The matrix (:mod:`alx.verify.configs`) proves that every configuration compiles and that the rows
together compile every line; the analysers read that code. A repository names the rows to analyse,
a few that between them compile every code line, and this module runs both analysers over every
source of each row twice: as the row configures it, and with the repository's ``disabled``
operations applied after the row's own - the assertions and the traces off - because a value only
a check reads is left unread only there. The named rows are also preprocessed, as the matrix
measures its coverage, and a code line none of them compiles fails the run: a set of rows that
stopped covering the repository would otherwise pass in silence.

clang-tidy reads each source the way the matrix's clang compiler sees the row - its target or host
flags, the platform's flags, the defines and the include folders - whichever toolchain the row
names, because the analyser is clang; its checks are the repository's ``.clang-tidy``. A line only
another compiler takes (``#if defined(__GNUC__) && !defined(__clang__)``) is cppcheck's alone.
cppcheck reads all the sources of a row in one run per option set the caller gives - one per
platform model, ``--platform=unix32`` for a 32-bit target and ``--platform=win64`` for the PC - with
the row's defines and include folders and the macros its compiler predefines that the sources
test, which cppcheck does not know (``defines``, by the row's toolchain word, ``host`` for a row
on the PC).

A finding is a diagnostic in one of the matrix's measured files; one in a fake or a vendor file is
not the repository's. The matrix's ``accept`` entries keep a finding by decision here as they do
for the compilers. ``report.json`` holds everything, ``findings.txt`` beside each row's
configuration the whole list and, with clang-tidy, ``compile_commands.json`` the rows as it read
them, configured, for a gate that reads a database; :mod:`alx.verify.gates.analyzers` fails on
any finding, any line no analysed row compiles and any run that was not every named row in both
variants.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from alx.verify import configs

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

SCHEMA = 1
CONFIGURED, DISABLED = "configured", "disabled"
VARIANTS: tuple[str, ...] = (CONFIGURED, DISABLED)
"""Every row runs as configured and with the checks disabled, in this order."""

CPPCHECK_TEMPLATE = "--template={file}:{line}:{column}: {severity}: {message} [{id}]"
"""cppcheck's findings in the one-line form of a compiler's, which :func:`parse_findings` reads."""

_FINDING = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<kind>fatal error|error|warning|style|performance|portability|information|note): "
    r"(?P<text>.*)$"
)


@dataclass(frozen=True)
class Tidy:
    """clang-tidy on this machine.

    The executable, the repository's configuration file, the compiler word of the matrix whose view
    of a row it takes, and flags of the analysis's own before the row's - an ``-include`` of what
    only the analyser needs to be told, as cppcheck is told through a ``--library`` file.
    """

    exe: Path
    config: Path
    compiler: str = "clang"
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Cppcheck:
    """cppcheck on this machine.

    The options of every run, one option set per run of a row, and the macros a compiler
    predefines that the sources test, by toolchain word (``host`` for a row on the PC).
    """

    exe: Path
    options: tuple[str, ...] = ()
    runs: tuple[tuple[str, ...], ...] = ((),)
    defines: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """One analyser diagnostic: which analyser, where, what kind, and the message with its check."""

    tool: str
    file: str
    line: int
    kind: str
    text: str

    def render(self) -> str:
        """Return the finding as one line."""
        return f"{self.file}:{self.line}: {self.tool} {self.kind}: {self.text}"


def parse_findings(tool: str, output: str) -> list[Finding]:
    """Return every diagnostic of an analyser's output; notes belong to the finding before them."""
    found = []
    for line in output.splitlines():
        m = _FINDING.match(line.strip())
        if m and m["kind"] != "note":
            found.append(Finding(tool, m["file"], int(m["line"]), m["kind"], m["text"]))
    return found


def compile_flags(
    compiler: configs.Compiler,
    platform: configs.Platform,
    includes: Iterable[Path],
    defines: Iterable[str] = (),
) -> list[str]:
    """Return a compiler's flags for one row: the view an analyser takes.

    No executable, no mode and no warnings; the order is :func:`alx.verify.configs.compile_argv`'s.
    """
    return [
        *compiler.base_argv(platform)[1:],
        *[f"-D{define}" for define in (*defines, *platform.defines)],
        *[f"-I{folder}" for folder in includes],
        *[f"-I{folder}" for folder in platform.includes],
    ]


@dataclass(frozen=True)
class _Job:
    row: configs.Row
    variant: str
    tool: str
    argv: tuple[str, ...]
    env: Mapping[str, str] | None


def _jobs(
    matrix: configs.Matrix,
    rows: Sequence[configs.Row],
    out: Path,
    tidy: Tidy | None,
    cppcheck: Cppcheck | None,
    disabled: Sequence[configs.Operation],
) -> list[_Job]:
    jobs: list[_Job] = []
    sources = [str(s) for s in matrix.sources]
    for row in rows:
        platform = matrix.platform(row.words["platform"])
        own = matrix.compilers[row.words["toolchain"]]
        for variant in VARIANTS:
            extra = disabled if variant == DISABLED else ()
            folder = configs.write_config(matrix, row, out / row.name / variant, extra)
            includes = [folder, *matrix.includes, *matrix.matrix.includes]
            defines = matrix.matrix.defines
            if variant == CONFIGURED:
                jobs.extend(
                    _Job(
                        row,
                        variant,
                        "preprocess",
                        tuple(
                            configs.compile_argv(own, platform, s, includes, "coverage", defines)
                        ),
                        own.env,
                    )
                    for s in matrix.sources
                )
            if tidy is not None:
                compiler = matrix.compilers[tidy.compiler]
                flags = compile_flags(compiler, platform, includes, defines)
                jobs.extend(
                    _Job(
                        row,
                        variant,
                        "clang-tidy",
                        (
                            str(tidy.exe),
                            "--quiet",
                            f"--config-file={tidy.config}",
                            source,
                            "--",
                            *(["-xc"] if source.endswith(".h") else []),
                            *tidy.flags,
                            *flags,
                        ),
                        compiler.env,
                    )
                    for source in sources
                )
            if cppcheck is not None:
                key = "host" if platform.host else row.words["toolchain"]
                predefined = [f"-D{d}" for d in cppcheck.defines.get(key, ())]
                flags = [
                    f
                    for f in compile_flags(own, platform, includes, defines)
                    if f[:2] in ("-D", "-I")
                ]
                jobs.extend(
                    _Job(
                        row,
                        variant,
                        "cppcheck",
                        (
                            str(cppcheck.exe),
                            *cppcheck.options,
                            *run,
                            CPPCHECK_TEMPLATE,
                            *predefined,
                            *flags,
                            *sources,
                        ),
                        None,
                    )
                    for run in cppcheck.runs
                )
    return jobs


def compile_database(
    matrix: configs.Matrix, rows: Sequence[configs.Row], out: Path, word: str = "clang"
) -> list[dict[str, Any]]:
    """Return the rows' compile database as configured, through one compiler word's view.

    Each source of each row once, as clang-tidy reads it, for a gate that reads a database
    (:mod:`alx.verify.gates.c_types`); the configuration folders are the ones
    :func:`run_analysis` writes under ``out``.
    """
    compiler = matrix.compilers[word]
    entries: list[dict[str, Any]] = []
    for row in rows:
        platform = matrix.platform(row.words["platform"])
        folder = out / row.name / CONFIGURED / "config"
        includes = [folder, *matrix.includes, *matrix.matrix.includes]
        flags = compile_flags(compiler, platform, includes, matrix.matrix.defines)
        for source in matrix.sources:
            header = ["-xc"] if source.suffix == ".h" else []
            argv = [str(compiler.exe), *header, *flags, "-c", str(source)]
            entries.append({"directory": str(out), "file": str(source), "arguments": argv})
    return entries


@dataclass
class RunResult:
    """What one row produced in one variant: its findings and the ones kept by decision."""

    row: configs.Row
    variant: str
    findings: list[Finding] = field(default_factory=list)
    accepted: list[tuple[Finding, str]] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        """Return the run's record for ``report.json``."""
        return {
            "row": self.row.name,
            "variant": self.variant,
            "findings_total": len(self.findings),
            "findings": [f.render() for f in self.findings[: configs.FINDINGS_KEPT]],
            "accepted_total": len(self.accepted),
            "accepted": [
                f"{f.render()} - {why}" for f, why in self.accepted[: configs.FINDINGS_KEPT]
            ],
        }


def run_analysis(
    matrix: configs.Matrix,
    rows: Sequence[configs.Row],
    out: Path,
    *,
    tidy: Tidy | None,
    cppcheck: Cppcheck | None,
    disabled: Sequence[configs.Operation] = (),
    only: Sequence[str] = (),
    runner: Callable[..., subprocess.CompletedProcess[str]] = configs.run,
    workers: int | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Analyse the named rows, write ``report.json`` and ``report.md`` under ``out``, return it.

    ``only`` selects among the rows, by the start of an id or a name, as the matrix's does.
    Every job - a source preprocessed, a source through clang-tidy, a row through cppcheck - is
    one task in one pool, so the slowest row does not idle the machine.
    """
    selected = configs.select(rows, only)
    if not selected:
        msg = f"no analysed row matches {list(only)}"
        raise configs.MatrixError(msg)
    matrix.check(selected)
    jobs = _jobs(matrix, selected, out, tidy, cppcheck, disabled)
    results = {(r.name, v): RunResult(r, v) for r in selected for v in VARIANTS}
    root = matrix.root.resolve()
    measured = {path.resolve().relative_to(root).as_posix() for path in matrix.measured}
    accepts = matrix.matrix.accepted
    cache: dict[str, str | None] = {}
    emitted: dict[str, set[int]] = {}

    def execute(job: _Job) -> str:
        done = runner(job.argv, job.env)
        return done.stdout + done.stderr

    with ThreadPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        for job, output in zip(jobs, pool.map(execute, jobs), strict=True):
            if job.tool == "preprocess":
                for file, numbers in configs.emitted_lines(output).items():
                    key = configs._key(root, file, cache)
                    if key is not None:
                        emitted.setdefault(key, set()).update(numbers)
                continue
            result = results[(job.row.name, job.variant)]
            for finding in parse_findings(job.tool, output):
                where = configs._key(root, finding.file, cache)
                if where not in measured:
                    continue  # a fake's or a vendor file's, not the repository's
                why = next((a.why for a in accepts if a.covers(where, finding.text)), None)
                if why is None:
                    result.findings.append(finding)
                else:
                    result.accepted.append((finding, why))
    for result in results.values():
        # every source includes the shared headers, so a header's finding comes once per source
        result.findings = list(dict.fromkeys(result.findings))
        result.accepted = list(dict.fromkeys(result.accepted))
        folder = out / result.row.name / result.variant
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "findings.txt").write_text(
            "".join(f"{f.render()}\n" for f in result.findings), encoding="utf-8", newline="\n"
        )
        if log is not None:
            log(f"{len(result.findings):5} finding(s)  {result.row.name} {result.variant}")
    totals, uncovered, exempted = configs.coverage_report(matrix, emitted)
    report: dict[str, Any] = {
        "source": {
            "kind": "analyzers",
            "schema": SCHEMA,
            "captured": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": "alx.verify.analyzers",
            "arguments": {"only": list(only), "rows": [r.name for r in selected], "of": len(rows)},
            "tools": {
                name: _version(tool.exe, runner)
                for name, tool in (("clang-tidy", tidy), ("cppcheck", cppcheck))
                if tool is not None
            },
            "heads": dict(matrix.heads),
        },
        "runs": [results[(r.name, v)].as_json() for r in selected for v in VARIANTS],
        "coverage": totals,
        "uncovered": {k: [g.as_json() for g in v] for k, v in uncovered.items()},
        "exempted": {k: [g.as_json() for g in v] for k, v in exempted.items()},
    }
    out.mkdir(parents=True, exist_ok=True)
    if tidy is not None:
        database = compile_database(matrix, selected, out, tidy.compiler)
        text = json.dumps(database, indent=1)
        (out / "compile_commands.json").write_text(text, encoding="utf-8", newline="\n")
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8", newline="\n")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return report


def _version(exe: Path, runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    """Return the line of ``--version`` that names the version."""
    lines = [ln.strip() for ln in runner([str(exe), "--version"], None).stdout.splitlines()]
    return next((ln for ln in lines if "version" in ln.lower()), lines[0] if lines else str(exe))


def render_markdown(report: Mapping[str, Any]) -> str:
    """Return a report for a person: every row and variant, its findings, the uncovered lines."""
    totals, arguments = report["coverage"], report["source"]["arguments"]
    lines = [
        "# Static analysis over the matrix",
        "",
        f"{len(arguments['rows'])} of {arguments['of']} rows, each {' and '.join(VARIANTS)}. "
        f"Code lines: {totals['code_lines']}, compiled by these rows {totals['covered']}, "
        f"exempt {totals['exempt']}, uncovered {totals['uncovered']}.",
        "",
        "| row | variant | findings | kept by decision |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| `{r['row']}` | {r['variant']} | {r['findings_total']} | {r['accepted_total']} |"
        for r in report["runs"]
    ]
    for r in report["runs"]:
        if r["findings_total"]:
            shown = r["findings"][:40]
            lines += ["", f"## {r['row']} {r['variant']}", "", "```", *shown]
            lines += [*(["..."] if r["findings_total"] > len(shown) else []), "```"]
    lines += ["", "## Lines no analysed row compiled", ""]
    lines += configs._gap_lines(report["uncovered"]) or ["none"]
    kept = dict.fromkeys(line for r in report["runs"] for line in r["accepted"])
    lines += ["", "## Findings kept by decision", ""]
    lines += [f"- {line}" for line in kept] or ["none"]
    return "\n".join(lines) + "\n"
