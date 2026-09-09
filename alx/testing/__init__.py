# SPDX-License-Identifier: MIT
"""Helpers for the pytest suites that use the library (the ``numpy.testing`` idea): evidence.

* traceability hook: the proof token in a test's name (``test_ALX<key>_P<n>_...``) is the primary
  trace link; it is mirrored into the junit XML as ``<property name="proof">``, every
  ``@pytest.mark.req("ALX-<key>-P<n>")`` marker as ``<property name="req">`` (``junit_family =
  xunit1``)
* ``run_dir``: the per-run evidence directory (``ALX_HIL_RUN_DIR`` from the launcher, else a
  timestamp)
* ``git_head``: short HEAD of a repo (device repo, submodules) for the run's identity record

Load from a suite's ``conftest.py``, one of two ways::

    pytest_plugins = ("alx.testing",)           # when the conftest does not import alx.testing

    from alx import testing                     # when it does (for run_dir, git_head):
    def pytest_configure(config):               # register the imported module; loading it by
        config.pluginmanager.register(testing, "alx.testing")  # name afterwards = rewrite warning

Nothing here touches hardware or firmware; host suites and bench suites use it alike.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

PROOF_RE = re.compile(r"ALX(\d+)_P(\d+)")


def pytest_collection_modifyitems(items) -> None:
    """Mirror the proof token of every test name and every ``req`` marker into junit properties."""
    for item in items:
        match = PROOF_RE.search(item.name)
        if match:
            item.user_properties.append(("proof", f"ALX-{match.group(1)}-P{match.group(2)}"))
        for mark in item.iter_markers(name="req"):
            for rid in mark.args:
                item.user_properties.append(("req", rid))


def git_head(path: str | Path) -> str:
    """Return the short HEAD of the repo at ``path``, ``"?"`` when not a repo or git is missing."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:  # pragma: no cover
        return "?"


def run_dir(test_dir: str | Path, env_var: str = "ALX_HIL_RUN_DIR") -> Path:
    """Return the per-run output directory: ``ALX_HIL_RUN_DIR`` if set, else a timestamped one.

    Default ``<test_dir>/build/runs/<yymmddHHMMSS>/``; never overwritten, one directory per run. The
    directory is only named here; the session owner creates it.
    """
    env = os.environ.get(env_var)
    return Path(env) if env else Path(test_dir) / "build" / "runs" / time.strftime("%y%m%d%H%M%S")
