# SPDX-License-Identifier: MIT
"""Verification lanes of the Auralix Python Library; the process is described in tests/README.md.

Run inside the repository's uv environment (``uv sync --locked --extra dev`` once)::

    uv run nox -l                  list the lanes
    uv run nox                     the default set: build, test, analyze, sanitize, coverage
    uv run nox -s mutate           report-only mutation run (slow, on demand)
    uv run nox -s matrix           the suite on every supported interpreter (uv fetches them)
    uv run nox -s test -- -k cli   pass arguments through to pytest

The sessions are named after the pipeline stages (``alx.verify.lanes.STAGES``) and run in the
repository's own environment, no second environment per lane; only ``matrix`` creates one venv per
interpreter under ``.nox/``. Evidence lands under ``build/<stage>/``; the dev lane (``test``) writes
to ``build/`` directly, like the C library.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import nox

from alx.verify import lanes

nox.options.default_venv_backend = "none"
nox.options.sessions = list(lanes.DEFAULT_SESSIONS)

ROOT = Path(__file__).parent
PYTHON = sys.executable
PYTHONS = ["3.11", "3.12", "3.13"]


def _uv() -> str:
    """Return the uv executable; uv is the environment standard of every Auralix repository."""
    uv = shutil.which("uv")
    if uv is None:
        msg = "uv not found on PATH (install uv; it manages the environment of every lane)"
        raise FileNotFoundError(msg)
    return uv


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@nox.session
def build(session: nox.Session) -> None:
    """BUILD - HOST: byte-compile, sdist + wheel, metadata and content checks, install smoke."""
    out = ROOT / "build" / lanes.BUILD_PRODUCT
    shutil.rmtree(out, ignore_errors=True)
    lanes.evidence_dir(ROOT, lanes.BUILD_PRODUCT)
    uv = _uv()
    session.run(PYTHON, "-m", "compileall", "-q", "alx")
    session.run(uv, "build", "--quiet", "--out-dir", str(out), ".", external=True)
    wheels = sorted(out.glob("*.whl"))
    sdists = sorted(out.glob("*.tar.gz"))
    session.run(PYTHON, "-m", "twine", "check", "--strict", *map(str, wheels + sdists))
    session.run(PYTHON, "-m", "check_wheel_contents", *map(str, wheels))
    smoke = out / "smoke"
    session.run(uv, "venv", "-q", str(smoke), external=True)
    session.run(uv, "pip", "install", "-q", "--python", str(smoke), str(wheels[0]), external=True)
    session.run(
        str(_venv_python(smoke)),
        "-c",
        "import alx, alx.debug_probe, alx.debug_probe.jlink, alx.psu.owon_p4603, alx.c_lib.cli, "
        "alx.c_lib.trace, alx.fw.live_watch, alx.serial_logger, alx.verify, alx.verify.evidence, "
        "alx.verify.lanes, alx.verify.ascii_gate, alx.verify.readme_gate, alx.verify.c_style, "
        "alx.verify.coverage_gate, alx.verify.mutation; print('installed alx', alx.__version__)",
        external=True,
    )
    session.log(f"BUILD CLEAN - {[w.name for w in wheels + sdists]} in {out}")


@nox.session
def test(session: nox.Session) -> None:
    """TEST - HOST: the offline suite in random order; evidence build/pytest_report.xml + .html."""
    session.run(PYTHON, "-m", "pytest", *session.posargs)


@nox.session(python=PYTHONS, venv_backend="uv|virtualenv", reuse_venv=True)
def matrix(session: nox.Session) -> None:
    """TEST - HOST on every supported interpreter; evidence under build/matrix/py<ver>/."""
    session.install("-e", ".[test]")
    out = lanes.evidence_dir(ROOT, "matrix", f"py{str(session.python).replace('.', '')}")
    session.run(
        "python",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *lanes.pytest_reports(out),
        *session.posargs,
    )


@nox.session
def analyze(session: nox.Session) -> None:
    """ANALYZE: 0 style (ruff, codespell, ASCII, README), 1 types (mypy), 2 deps (pip-audit)."""
    out = lanes.evidence_dir(ROOT, "analyze")
    session.log("Stage 0: ruff format --check, ruff check, codespell, ascii gate, readme gate")
    session.run(PYTHON, "-m", "ruff", "format", "--check", ".")
    session.run(PYTHON, "-m", "ruff", "check", ".")
    session.run(
        PYTHON,
        "-m",
        "ruff",
        "check",
        ".",
        "--output-format",
        "junit",
        "--output-file",
        str(out / "ruff.xml"),
    )
    session.run(PYTHON, "-m", "codespell_lib")
    session.run(PYTHON, "-m", "alx.verify.ascii_gate", ".", "--out", str(out / "ascii_gate.txt"))
    session.run(PYTHON, "-m", "alx.verify.readme_gate", ".", "--out", str(out / "readme_gate.txt"))
    session.log("Stage 1: mypy (strict on the package, tests checked)")
    session.run(PYTHON, "-m", "mypy", "--junit-xml", str(out / "mypy.xml"))
    session.log("Stage 2: pip-audit over the locked dependencies (uv.lock)")
    requirements = out / "requirements.txt"
    session.run(
        _uv(),
        "export",
        "--locked",
        "--extra",
        "dev",
        "--no-emit-project",
        "--no-hashes",
        "--quiet",
        "-o",
        str(requirements),
        external=True,
    )
    session.run(
        PYTHON,
        "-m",
        "pip_audit",
        "--disable-pip",
        "--no-deps",
        "--progress-spinner",
        "off",
        "-r",
        str(requirements),
        "--format",
        "json",
        "--output",
        str(out / "pip_audit.json"),
    )
    session.log(f"ANALYZE CLEAN - evidence in {out}")


@nox.session
def sanitize(session: nox.Session) -> None:
    """SANITIZE: the same suite under -X dev (allocator hooks, tracemalloc, warnings as errors)."""
    out = lanes.evidence_dir(ROOT, "sanitize")
    session.run(
        PYTHON,
        "-X",
        "dev",
        "-X",
        "faulthandler",
        "-X",
        "tracemalloc=5",
        "-W",
        "error",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *lanes.pytest_reports(out),
        *session.posargs,
    )


@nox.session
def coverage(session: nox.Session) -> None:
    """COVERAGE: branch coverage over the suite; gate = 100 % lines and branches per file."""
    out = ROOT / "build" / "coverage"
    shutil.rmtree(out, ignore_errors=True)
    lanes.evidence_dir(ROOT, "coverage")
    session.run(
        PYTHON,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "--cov",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=xml",
        "--cov-report=html",
        "--cov-report=json",
        *lanes.pytest_reports(out),
        *session.posargs,
    )
    session.run(
        PYTHON,
        "-m",
        "alx.verify.coverage_gate",
        str(out / "coverage.xml"),
        "--min",
        "100",
        "--out",
        str(out / "coverage_gate.txt"),
    )


@nox.session
def mutate(session: nox.Session) -> None:
    """MUTATE (report-only): universalmutator mutants of each module run against its tests."""
    sources = session.posargs or [
        p.relative_to(ROOT).as_posix() for p in sorted((ROOT / "alx").rglob("*.py"))
    ]
    session.run(
        PYTHON,
        "-m",
        "alx.verify.mutation",
        "--out",
        str(ROOT / "build" / "mutate"),
        "--sample",
        "100",
        *sources,
    )
