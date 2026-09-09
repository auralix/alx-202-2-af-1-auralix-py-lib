# SPDX-License-Identifier: MIT
"""The lane vocabulary shared by every Auralix repository's ``noxfile.py``.

The verification pipeline has the same stages in every repository, whatever the language: BUILD,
TEST, ANALYZE, SANITIZE, COVERAGE and MUTATE on the host, then BUILD and TEST on the target. A lane
is one stage run as one nox session named after it (``nox -s coverage``), and its evidence lands
under ``build/<stage>/``; the dev lane, a plain ``pytest``, writes to ``build/`` itself. This module
holds that vocabulary once, so a noxfile states only what is specific to its repository: tools,
sources, flags.

    from alx.verify import lanes

    nox.options.sessions = list(lanes.DEFAULT_SESSIONS)
    out = lanes.evidence_dir(ROOT, "coverage")  # build/coverage/, created
    session.run("pytest", *lanes.pytest_reports(out))  # junit + html into it
    llvm = lanes.tool("ALX_LLVM_DIR", "C:/Program Files/LLVM/bin")  # machine configuration

Tool locations are machine configuration, never repository content: an environment variable names
the instance on this machine, the noxfile carries the default of the reference bench. A lane that
needs a tool fails when the tool is missing; it never skips.
"""

from __future__ import annotations

import os
from pathlib import Path

STAGES: tuple[str, ...] = ("build", "test", "analyze", "sanitize", "coverage", "mutate")
"""The host stages in pipeline order = the nox session names of every repository."""

DEFAULT_SESSIONS: tuple[str, ...] = ("build", "test", "analyze", "sanitize", "coverage")
"""What a bare ``nox`` runs; MUTATE is slow and runs on demand."""

BUILD_PRODUCT = "dist"
"""The BUILD stage keeps its product folder (``build/dist/``) instead of a report folder."""


def evidence_dir(root: Path, lane: str, *parts: str) -> Path:
    """Return ``root/build/<lane>/<parts...>``, created.

    ``lane`` is a stage name, a variant of one (``"matrix"`` with the interpreter as a part,
    ``"sanitize"`` with ``"asan"``) or :data:`BUILD_PRODUCT`.
    """
    out = root / "build" / lane
    for part in parts:
        out = out / part
    out.mkdir(parents=True, exist_ok=True)
    return out


def pytest_reports(out: Path) -> list[str]:
    """Return the pytest options that write the junit and the html report into ``out``."""
    return [
        f"--junitxml={(out / 'pytest_report.xml').as_posix()}",
        f"--html={(out / 'pytest_report.html').as_posix()}",
        "--self-contained-html",
    ]


def tool(env_var: str, default: str | Path) -> Path:
    """Return a tool's location: the environment variable ``env_var`` when set, else ``default``.

    Raises ``FileNotFoundError`` naming the variable when the location does not exist, so the lane
    that needs the tool fails with the fix in the message instead of skipping.
    """
    path = Path(os.environ.get(env_var) or default)
    if not path.exists():
        msg = f"tool not found: {path} (install it or set {env_var})"
        raise FileNotFoundError(msg)
    return path
