# SPDX-License-Identifier: MIT
"""Evidence of a pytest run: proof tokens into junit, the per-run folder, repository heads.

* traceability hook: the proof token in a test's name (``test_ALX<key>_P<n>_...``) is the primary
  trace link; it is mirrored into the junit XML as ``<property name="proof">``, every
  ``@pytest.mark.req("ALX-<key>-P<n>")`` marker as ``<property name="req">`` (``junit_family =
  xunit1``)
* ``run_dir``: the per-run evidence directory (``ALX_HIL_RUN_DIR`` from the launcher, else a
  timestamp)
* ``git_head``: short HEAD of a repo (device repo, submodules) for the run's identity record,
  ``-dirty`` appended when its tracked files differ from HEAD
* random order on record: when pytest-randomly is active, its seed lands in the junit XML as the
  testsuite property ``randomly_seed`` (the seed policy: random every run, never fixed, always
  recorded; reproduce with ``--randomly-seed=<n>``)
* ``bundle``: a run's evidence copied into a release folder, once the release image is proven to
  be the image the run tested (the sha256 the launcher wrote into the run's ``image.json``)

Load from a suite's ``conftest.py``, one of two ways::

    pytest_plugins = ("alx.verify.evidence",)  # when the conftest does not import this module

    from alx.verify import evidence  # when it does (for run_dir, git_head):


    def pytest_configure(config):  # register the imported module; loading it by
        config.pluginmanager.register(evidence, "alx.verify.evidence")  # name = rewrite warning

Nothing here touches hardware or firmware; host suites and bench suites use it alike.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pytest

PROOF_RE = re.compile(r"ALX(\d+)_P(\d+)")


def pytest_sessionstart(session: pytest.Session) -> None:
    """Record pytest-randomly's seed as the junit testsuite property ``randomly_seed``.

    Only when both are active: pytest-randomly (the option exists) and the junit report
    (``--junitxml``). The module stays importable without pytest (the install smoke of BUILD imports
    it), so the junit plugin is reached through the plugin manager, not imported.
    """
    config = session.config
    seed = config.getoption("randomly_seed", default=None)
    junitxml = config.pluginmanager.get_plugin("junitxml")  # a pytest builtin, always registered
    xml = config.stash.get(getattr(junitxml, "xml_key"), None)  # noqa: B009 - reached, not imported
    if seed is None or xml is None:
        return
    xml.add_global_property("randomly_seed", str(seed))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mirror the proof token of every test name and every ``req`` marker into junit properties."""
    for item in items:
        match = PROOF_RE.search(item.name)
        if match:
            item.user_properties.append(("proof", f"ALX-{match.group(1)}-P{match.group(2)}"))
        for mark in item.iter_markers(name="req"):
            for rid in mark.args:
                item.user_properties.append(("req", rid))


def git_head(path: str | Path) -> str:
    """Return the short HEAD of the repo at ``path``, ``"?"`` when not a repo or git is missing.

    ``-dirty`` is appended when tracked files differ from HEAD, staged or not; untracked files do
    not count. That is the mark ``git describe --dirty`` gives, so a run of a modified tree is never
    recorded as the commit it started from.
    """
    try:
        head = _git(path, "rev-parse", "--short", "HEAD")
        changes = _git(path, "status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError):  # git missing, or not a repository
        return "?"
    return f"{head}-dirty" if changes else head


def _git(path: str | Path, *args: str) -> str:
    """Run one read-only git query in ``path`` and return its output, stripped.

    ``--no-optional-locks``: status would otherwise write its index refresh back, and a query that
    records a run must not change the tree it records.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell; a read-only git query
        # S607 waived on the next line: the git on PATH is the developer's own, and the wrong
        # one can only misreport a hash - this query writes nothing and decides nothing.
        ["git", "--no-optional-locks", "-C", str(path), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def run_dir(test_dir: str | Path, env_var: str = "ALX_HIL_RUN_DIR") -> Path:
    """Return the per-run output directory: ``ALX_HIL_RUN_DIR`` if set, else a timestamped one.

    Default ``<test_dir>/build/runs/<yymmddHHMMSS>/``; never overwritten, one directory per run. The
    directory is only named here; the session owner creates it.
    """
    env = os.environ.get(env_var)
    return Path(env) if env else Path(test_dir) / "build" / "runs" / time.strftime("%y%m%d%H%M%S")


def bundle(
    run_dir: str | Path, dest: str | Path, image: str | Path, exclude: Iterable[str] = ()
) -> list[Path]:
    """Copy a run's evidence into ``dest``, once ``image`` is proven to be the image the run tested.

    The launcher writes ``image.json`` into the run folder with the sha256 of the image it flashed;
    the copy is refused when ``image`` hashes differently, so a release never carries the test run
    of another build. Files whose name is in ``exclude`` are left out (a UART transcript is
    megabytes that prove nothing the reports do not); everything else is copied as the run wrote
    it, folders included, into ``dest``, which must not exist yet. Returns the copied files.
    """
    run, target, released = Path(run_dir), Path(dest), Path(image)
    record = run / "image.json"
    if not record.is_file():
        raise ValueError(f"{run} holds no image.json: not a run that flashed an image")
    recorded: str = json.loads(record.read_text(encoding="utf-8")).get("sha256", "")
    actual = hashlib.sha256(released.read_bytes()).hexdigest()
    if recorded.lower() != actual:
        msg = f"{released.name} is not the image this run tested: run {recorded[:12]}, image {actual[:12]}"  # noqa: E501 - one message, both hashes
        raise ValueError(msg)
    if target.exists():
        raise ValueError(f"{target} exists: evidence is written once, never overwritten")
    skipped = set(exclude)
    copied = []
    for path in sorted(p for p in run.rglob("*") if p.is_file()):
        if path.name in skipped:
            continue
        out = target / path.relative_to(run)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        copied.append(out)
    return copied
