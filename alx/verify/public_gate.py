# SPDX-License-Identifier: MIT
r"""Public-repo gate: no private vocabulary and no non-ASCII byte reaches a public repository.

A public repository is world-readable from its first push, and one leaked client or product name
cannot be taken back. This gate reads a VOCABULARY of forbidden literals and patterns and reports
every line that carries one - in the working tree, and in commit messages too, because a message
is exactly as public as the file it describes.

The vocabulary is NOT in this repository and never can be: the list itself names what must stay
private. It is a JSON file kept wherever the organisation keeps private policy, and the gate is
told where by ``--words`` or the ``ALX_GATE_WORDS`` environment variable. A gate with no
vocabulary FAILS: a machine that simply lacks the file would otherwise report PASS over a tree
full of findings, which is worse than having no gate at all. Usage::

    python -m alx.verify.public_gate <root> [--words <vocab.json>] [--base origin/master]
        [--exclude <name-or-relative-path>]... [--commits-only] [--history [--diff-only]]
        [--out <report.txt>]

Default mode scans the working tree plus the messages of ``base..HEAD``. ``--history`` scans every
commit in that range instead - each commit's whole tree, or with ``--diff-only`` only the lines it
adds - and marks whether a finding is still present at HEAD or lives only in history, which is the
question that has to be answered before publishing a full history.

Binary files are skipped by looking for a NUL byte, deliberately NOT by an allowlist of text
suffixes the way :mod:`alx.verify.ascii_gate` selects its files. A leak gate must not silently skip
a file type nobody thought to list; measured on one consumer, an allowlist skipped four real text
files while exactly one file in the tree was binary. The number skipped is reported so the choice
never becomes invisible.

Vocabulary format:

.. code-block:: json

    {
      "groups": [
        {"why": "why these are private", "literals": ["Widget"], "patterns": ["\\bW-[0-9]+"]}
      ],
      "excludes": ["Vendor"],
      "decided_public": [{"what": "a name once listed", "when": "2026-01-01", "why": "released"}]
    }

``literals`` match as plain substrings, which is what catches a name inside a longer identifier
(``Widget`` in ``WidgetBringUp.c``); word boundaries would miss exactly that, so they are not used
unless an entry asks for them. ``patterns`` are regular expressions, for entries a bare substring
would over-match. ``excludes`` name vendor trees the repository did not write, in the same form
the other gates take them. ``decided_public`` is documentation only: names once listed and since
released, recorded so they are not added back by mistake.

Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

WORDS_ENV = "ALX_GATE_WORDS"
SKIP_DIRS = frozenset(
    {
        ".git", ".venv", ".nox", ".tox", "build", "dist", "__pycache__", ".mypy_cache",
        ".ruff_cache", ".hypothesis", ".pytest_cache", "node_modules",
    }
)  # fmt: skip
SKIP_NAMES = frozenset({"uv.lock"})  # generated, and it quotes every dependency URL
NUL_PROBE = 8192  # bytes of a file inspected for the NUL that marks it binary
ECHO_WIDTH = 100  # how much of an offending line is quoted back


def echo(line: str) -> str:
    """Return the quoted-back form of an offending line: trimmed, and ASCII whatever went in.

    A file that is not ASCII is exactly what this gate exists to find, and its line reaches here
    decoded with replacement characters. Escaping them keeps the report writable as ASCII and
    printable on a console that is not UTF-8.
    """
    return line.strip()[:ECHO_WIDTH].encode("ascii", errors="backslashreplace").decode("ascii")


@dataclass(frozen=True)
class Vocabulary:
    """The private list: forbidden literals, forbidden patterns, and trees to leave alone."""

    literals: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    excludes: tuple[str, ...] = ()

    def hits(self, label: str, text: str) -> list[str]:
        """Return one finding line per forbidden literal or pattern found in ``text``."""
        findings = []
        for number, line in enumerate(text.splitlines(), 1):
            found = [word for word in self.literals if word in line]
            found += [m.group(0) for m in (p.search(line) for p in self.patterns) if m]
            findings += [f"FORBIDDEN {f!r:16} {label}:{number}: {echo(line)}" for f in found]
        return findings


def load(path: str | Path) -> Vocabulary:
    """Read the vocabulary JSON; see the module docstring for the format."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    literals: list[str] = []
    patterns: list[re.Pattern[str]] = []
    for group in data.get("groups", []):
        literals.extend(group.get("literals", []))
        patterns.extend(re.compile(p) for p in group.get("patterns", []))
    return Vocabulary(tuple(literals), tuple(patterns), tuple(data.get("excludes", [])))


def vocabulary_path(explicit: str | None = None) -> Path:
    """Resolve ``--words`` or ``$ALX_GATE_WORDS``; a missing vocabulary is an error, not a skip."""
    raw = explicit or os.environ.get(WORDS_ENV)
    if not raw:
        msg = (
            f"no vocabulary: pass --words <file> or set {WORDS_ENV}. This gate never passes "
            "without one - a machine that simply lacks the list would report PASS over anything."
        )
        raise FileNotFoundError(msg)
    path = Path(raw)
    if not path.is_file():
        msg = f"vocabulary file not found: {path}"
        raise FileNotFoundError(msg)
    return path


def excluded(rel: Path, exclude: Iterable[str]) -> bool:
    """Return True when ``rel`` sits under an exclude: a folder name anywhere, or a path from root.

    The same rule :mod:`alx.verify.ascii_gate` applies, so one vendor list serves both gates.
    """
    parts = rel.parts
    posix = rel.as_posix()
    for item in exclude:
        norm = item.replace("\\", "/").strip("/")
        if norm in parts or posix == norm or posix.startswith(norm + "/"):
            return True
    return False


def is_binary(data: bytes) -> bool:
    """Return True when a NUL byte appears early - git's own heuristic for "this is not text"."""
    return b"\x00" in data[:NUL_PROBE]


def non_ascii(label: str, data: bytes) -> list[str]:
    """Return one finding line per line of ``data`` holding a byte above 0x7F."""
    if data.isascii():
        return []
    findings = []
    for number, line in enumerate(data.split(b"\n"), 1):
        if not line.isascii():
            findings.append(f"NON-ASCII {label}:{number}: {line[:80]!r}")
    return findings


def candidates(root: Path, exclude: Iterable[str]) -> Iterator[Path]:
    """Yield the files under ``root`` that are neither skipped nor excluded."""
    exclude = tuple(exclude)
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.is_file() and path.name not in SKIP_NAMES and not excluded(rel, exclude):
            yield path


def scan_tree(root: Path, vocab: Vocabulary, exclude: Iterable[str]) -> tuple[list[str], str]:
    """Scan the working tree; return the findings and a one-line description of what was read."""
    exclude = tuple(exclude)
    findings: list[str] = []
    scanned = 0
    binaries: list[str] = []
    for path in candidates(root, exclude):
        data = path.read_bytes()
        label = path.relative_to(root).as_posix()
        if is_binary(data):
            binaries.append(label)
            continue
        scanned += 1
        findings += non_ascii(label, data)
        findings += vocab.hits(label, data.decode("utf-8", errors="replace"))
    scope = f"{scanned} files"
    if exclude:
        scope += f", excluded: {', '.join(exclude)}"
    if binaries:
        shown = ", ".join(binaries[:3]) + ("..." if len(binaries) > 3 else "")
        scope += f", {len(binaries)} binary skipped ({shown})"
    return findings, scope


def git(root: Path, *args: str) -> str:
    """Run a read-only git query in ``root`` and return its stdout."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell; the caller builds it from constants
        ["git", "-C", str(root), *args],  # noqa: S607 - the git on PATH is the developer's own
        capture_output=True,
        text=True,
        check=True,
        errors="replace",
    ).stdout


def has_revision(root: Path, revision: str) -> bool:
    """Return True when ``revision`` resolves in ``root`` (a fresh clone has no origin/master)."""
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell; a read-only git query
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", revision],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def commits(root: Path, base: str, head: str) -> list[str]:
    """Return the commits of ``base..head``, oldest first."""
    return git(root, "rev-list", "--reverse", f"{base}..{head}").split()


def scan_messages(root: Path, vocab: Vocabulary, base: str, head: str) -> tuple[list[str], str]:
    """Scan the commit messages of ``base..head``."""
    if not has_revision(root, base):
        return [], f"base {base} not found, commit messages not scanned"
    findings: list[str] = []
    shas = commits(root, base, head)
    for sha in shas:
        body = git(root, "log", "-1", "--format=%B", sha)
        label = f"commit {sha[:7]}"
        findings += vocab.hits(label, body)
        findings += non_ascii(label, body.encode("utf-8"))
    return findings, f"{len(shas)} commit messages ({base}..{head})"


def added_lines(root: Path, sha: str) -> str:
    """Return the lines a commit adds, without the diff markers."""
    shown = git(root, "show", "--format=", "--no-color", sha)
    return "\n".join(
        line[1:]
        for line in shown.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def scan_history(
    root: Path, vocab: Vocabulary, base: str, head: str, *, diff_only: bool
) -> tuple[list[str], str]:
    """Scan every commit of ``base..head``: its message, and its tree or the lines it adds."""
    if not has_revision(root, base):
        return [], f"base {base} not found, history not scanned"
    findings: list[str] = []
    shas = commits(root, base, head)
    for sha in shas:
        label = f"{sha[:7]}"
        findings += vocab.hits(f"{label} MSG", git(root, "log", "-1", "--format=%B", sha))
        if diff_only:
            findings += vocab.hits(f"{label} ADDED", added_lines(root, sha))
        else:
            findings += vocab.hits(
                f"{label} TREE", git(root, "show", "--format=", "--no-color", sha)
            )
    mode = "added lines" if diff_only else "full diffs"
    return findings, f"{len(shas)} commits, {mode} ({base}..{head})"


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m alx.verify.public_gate", description=__doc__)
    parser.add_argument("root", help="repository root to scan")
    parser.add_argument("--words", help=f"vocabulary JSON; default ${WORDS_ENV}")
    parser.add_argument(
        "--base", default="origin/master", help="revision the commits are measured from"
    )
    parser.add_argument("--head", default="HEAD", help="revision the commits are measured to")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME_OR_PATH",
        help="vendor tree to leave alone, added to the vocabulary's own list; repeatable",
    )
    parser.add_argument("--commits-only", action="store_true", help="skip the working tree")
    parser.add_argument("--history", action="store_true", help="scan every commit of base..head")
    parser.add_argument("--diff-only", action="store_true", help="with --history: only added lines")
    parser.add_argument("--out", help="also write the report to this file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    args = _parse(argv)
    root = Path(args.root)
    vocab = load(vocabulary_path(args.words))
    exclude = (*vocab.excludes, *args.exclude)

    findings: list[str] = []
    scopes: list[str] = []
    if args.history:
        hits, scope = scan_history(root, vocab, args.base, args.head, diff_only=args.diff_only)
        findings += hits
        scopes.append(scope)
    else:
        if not args.commits_only:
            hits, scope = scan_tree(root, vocab, exclude)
            findings += hits
            scopes.append(scope)
        hits, scope = scan_messages(root, vocab, args.base, args.head)
        findings += hits
        scopes.append(scope)

    verdict = "FAIL" if findings else "PASS"
    lines = [f"PUBLIC GATE: {verdict} ({'; '.join(scopes)})", *findings]
    report = "\n".join(lines) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
