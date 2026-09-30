# SPDX-License-Identifier: MIT
"""Data source gate: every test data file identifies itself completely.

A file under ``data/`` at a verification root is JSON with two top-level keys: ``source``, the
record of where the data came from, and ``data``, the payload. Nothing about a data file's origin
lives outside it - not in Git, not in a run folder, not in a commit message. Whoever opens it knows
what it is, what produced it, from which firmware and which build, from which source tree and when.

The ``source`` record, every field present and none empty:

* ``kind``: what the file is (``cli_items``); ``schema``: the version of the file's own layout, so
  its readers can follow a change of shape.
* ``captured``: when, UTC, ISO 8601 to the second with the ``Z`` (``stamp()`` writes it).
* ``tool``, ``arguments``: what produced the file and how it was invoked, with the values that
  applied, so the capture can be repeated.
* ``device``: the identity the device itself reports - application and bootloader, artefact, name,
  version with its commit hash, image file. This is the strict identification: a modified tree can
  produce any image, so the heads alone are not.
* ``build``: what the firmware was when captured, as far as the bench can observe it (the control
  mode it was in).
* ``heads``: the repository heads the capture ran from, with the ``-dirty`` mark of
  ``alx.verify.evidence.git_head``.

``read`` and ``write`` are the two functions a repository's harness uses for its data files; both
refuse a file whose record is incomplete. Usage as a gate::

    python -m alx.verify.gates.data_source <root> [--out report.txt]

Exit code 0 = PASS, 1 = FAIL. Every finding is one ``<path>: <what>`` line.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import Any

DATA_DIR = "data"
KEYS = frozenset({"source", "data"})
REQUIRED = ("kind", "schema", "captured", "tool", "arguments", "device", "build", "heads")


def stamp() -> str:
    """Return the current UTC time the way ``captured`` records it."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty(value: Any) -> bool:
    """Return True when ``value`` says nothing: None, blank, or a container with nothing in it."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return not value or any(_empty(v) for v in value.values())
    if isinstance(value, list):
        return not value or any(_empty(v) for v in value)
    return False


def check_content(content: Any, where: str) -> list[str]:
    """Return the findings for one parsed data file; ``where`` names it in every line."""
    if not isinstance(content, dict) or set(content) != KEYS:
        return [f"{where}: a data file holds exactly two keys, source and data"]
    source = content["source"]
    if not isinstance(source, dict):
        return [f"{where}: source is a record of fields, not {type(source).__name__}"]
    findings = [f"{where}: source.{key} missing" for key in REQUIRED if key not in source]
    findings += [
        f"{where}: source.{key} is empty"
        for key in REQUIRED
        if key in source and _empty(source[key])
    ]
    findings += [
        f"{where}: source.{key} is not a field of the record"
        for key in source
        if key not in REQUIRED
    ]
    return findings


def check_file(path: Path, root: Path | None = None) -> list[str]:
    """Return the findings for the data file at ``path``, named relative to ``root`` when given."""
    where = path.relative_to(root).as_posix() if root else path.name
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):  # json.JSONDecodeError is a ValueError
        return [f"{where}: not JSON, so it carries no source record"]
    return check_content(content, where)


def files(root: str | Path) -> list[Path]:
    """Return every file under ``<root>/data/``, nested folders included, sorted."""
    folder = Path(root) / DATA_DIR
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []


def check(root: str | Path) -> list[str]:
    """Return every finding for the data files of the verification root ``root``."""
    return [finding for path in files(root) for finding in check_file(path, Path(root))]


def read(path: str | Path) -> dict[str, Any]:
    """Return the data file at ``path`` as ``{"source": ..., "data": ...}``.

    A file whose record is incomplete raises ``ValueError`` with the gate's findings: a consumer
    never works from data it cannot identify.
    """
    findings = check_file(Path(path))
    if findings:
        raise ValueError("\n".join(findings))
    content: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return content


def write(path: str | Path, source: dict[str, Any], data: Any) -> None:
    """Write ``source`` and ``data`` as the data file at ``path``, after checking the record.

    Two-space indented ASCII JSON with LF line ends, so the file is the same from every machine
    and its Git diff shows only what changed.
    """
    content = {"source": source, "data": data}
    findings = check_content(content, Path(path).name)
    if findings:
        raise ValueError("\n".join(findings))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes((json.dumps(content, indent=2) + "\n").encode("ascii"))


def main(argv: list[str] | None = None) -> int:
    """Command line entry; see the module docstring."""
    parser = argparse.ArgumentParser(
        prog="python -m alx.verify.gates.data_source", description=__doc__
    )
    parser.add_argument("root", type=Path, help="the verification root")
    parser.add_argument("--out", help="also write the report to this file")
    args = parser.parse_args(argv)
    findings = check(args.root)
    verdict = (
        f"FAIL ({len(findings)} finding(s))"
        if findings
        else f"PASS ({len(files(args.root))} files)"
    )
    report = "\n".join([f"DATA SOURCE GATE: {verdict}", *findings]) + "\n"
    sys.stdout.write(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="ascii")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
