# SPDX-License-Identifier: MIT
"""Verification lanes of the Auralix Python Library; the process is described in tests/README.md.

Run::

    nox -l                  list the lanes
    nox                     the default set: analyze, tests, sanitize, coverage, build
    nox -s mutate           report-only mutation run (slow, on demand)
    nox -s matrix           the suite on every supported interpreter (uv downloads missing ones)
    nox -s tests -- -k cli  pass arguments through to pytest

Every lane runs in its own virtual environment with ``.[dev]`` installed and writes its evidence
under ``build/<lane>/``; the dev lane (``tests``) writes to ``build/`` directly, like the C library.
Environments are reused between runs (``nox -r`` behaviour is the default here); ``nox --no-reuse``
rebuilds them.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import nox

nox.options.default_venv_backend = "uv|virtualenv"
nox.options.reuse_existing_virtualenvs = True
nox.options.sessions = ["analyze", "tests", "sanitize", "coverage", "build"]

ROOT = Path(__file__).parent
BUILD = ROOT / "build"
PYTHONS = ["3.10", "3.11", "3.12", "3.13"]


def _install(session: nox.Session) -> None:
    session.install("-e", ".[dev]")


def _reports(lane: str) -> list[str]:
    """Return the pytest report options that put junit + html evidence under build/<lane>/."""
    out = BUILD / lane
    out.mkdir(parents=True, exist_ok=True)
    return [
        f"--junitxml={out.as_posix()}/pytest_report.xml",
        f"--html={out.as_posix()}/pytest_report.html",
        "--self-contained-html",
    ]


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@nox.session
def analyze(session: nox.Session) -> None:
    """ANALYZE: 0 style (ruff, codespell, ASCII), 1 types (mypy), 2 dependencies (pip-audit)."""
    _install(session)
    out = BUILD / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    session.log("Stage 0: ruff format --check, ruff check, codespell, ascii gate")
    session.run("ruff", "format", "--check", ".")
    session.run("ruff", "check", ".")
    session.run(
        "ruff", "check", ".", "--output-format", "junit", "--output-file", str(out / "ruff.xml")
    )
    session.run("codespell")
    session.run("python", "-m", "alx.verify.ascii_gate", ".", "--out", str(out / "ascii_gate.txt"))
    session.log("Stage 1: mypy (strict on the package, tests checked)")
    session.run("mypy", "--junit-xml", str(out / "mypy.xml"))
    session.log("Stage 2: pip-audit over the lane environment")
    session.run(
        "pip-audit",
        "--progress-spinner",
        "off",
        "--skip-editable",
        "--format",
        "json",
        "--output",
        str(out / "pip_audit.json"),
    )
    session.log(f"ANALYZE CLEAN - evidence in {out}")


@nox.session
def tests(session: nox.Session) -> None:
    """TEST - HOST: the offline suite in random order; evidence build/pytest_report.xml + .html."""
    _install(session)
    session.run("pytest", *session.posargs)


@nox.session(python=PYTHONS, name="matrix")
def matrix(session: nox.Session) -> None:
    """TEST - HOST on every supported interpreter; evidence under build/matrix/py<ver>/."""
    _install(session)
    lane = f"matrix/py{str(session.python).replace('.', '')}"
    session.run("pytest", "-p", "no:cacheprovider", *_reports(lane), *session.posargs)


@nox.session
def sanitize(session: nox.Session) -> None:
    """SANITIZE: the same suite under -X dev (allocator hooks, tracemalloc, warnings as errors)."""
    _install(session)
    session.run(
        "python",
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
        *_reports("sanitize"),
        *session.posargs,
    )


@nox.session
def coverage(session: nox.Session) -> None:
    """COVERAGE: branch coverage over the suite; gate = 100 % lines and branches per file."""
    _install(session)
    out = BUILD / "cov"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    session.run(
        "pytest",
        "-p",
        "no:cacheprovider",
        "--cov",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=xml",
        "--cov-report=html",
        "--cov-report=json",
        *_reports("cov"),
        *session.posargs,
    )
    session.run(
        "python",
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
    _install(session)
    sources = session.posargs or [
        p.relative_to(ROOT).as_posix() for p in sorted((ROOT / "alx").rglob("*.py"))
    ]
    session.run(
        "python",
        "-m",
        "alx.verify.mutation",
        "--out",
        str(BUILD / "mutation"),
        "--sample",
        "100",
        *sources,
    )


@nox.session
def build(session: nox.Session) -> None:
    """BUILD - HOST: byte-compile, sdist + wheel, metadata and content checks, install smoke."""
    _install(session)
    out = BUILD / "dist"
    shutil.rmtree(out, ignore_errors=True)
    session.run("python", "-m", "compileall", "-q", "alx")
    session.run("python", "-m", "build", "--outdir", str(out), ".")
    wheels = sorted(out.glob("*.whl"))
    sdists = sorted(out.glob("*.tar.gz"))
    session.run("twine", "check", "--strict", *map(str, wheels + sdists))
    session.run("check-wheel-contents", *map(str, wheels))
    smoke = out / "smoke"
    uv = shutil.which("uv")
    if uv:
        session.run(uv, "venv", "-q", str(smoke), external=True)
        session.run(
            uv, "pip", "install", "-q", "--python", str(smoke), str(wheels[0]), external=True
        )
    else:
        session.run("python", "-m", "venv", str(smoke))
        session.run(
            str(_venv_python(smoke)), "-m", "pip", "install", "-q", str(wheels[0]), external=True
        )
    session.run(
        str(_venv_python(smoke)),
        "-c",
        "import alx, alx.debug_probe, alx.debug_probe.jlink, alx.psu.owon_p4603, alx.c_lib.cli, "
        "alx.c_lib.trace, alx.fw.live_watch, alx.serial_logger, alx.testing, alx.verify; "
        "print('installed alx', alx.__version__)",
        external=True,
    )
    session.log(f"BUILD CLEAN - {[w.name for w in wheels + sdists]} in {out}")
