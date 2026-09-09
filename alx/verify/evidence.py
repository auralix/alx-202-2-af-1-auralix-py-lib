# SPDX-License-Identifier: MIT
"""Evidence of a pytest run: proof tokens into junit, the per-run folder, repository heads.

* traceability hook: the proof token in a test's name (``test_ALX<key>_P<n>_...``) is the primary
  trace link; it is mirrored into the junit XML as ``<property name="proof">``, every
  ``@pytest.mark.req("ALX-<key>-P<n>")`` marker as ``<property name="req">`` (``junit_family =
  xunit1``)
* ``run_dir``: the per-run evidence directory (``ALX_HIL_RUN_DIR`` from the launcher, else a
  timestamp)
* ``git_head``: short HEAD of a repo (device repo, submodules) for the run's identity record
* random order on record: when pytest-randomly is active, its seed lands in the junit XML as the
  testsuite property ``randomly_seed`` (the seed policy: random every run, never fixed, always
  recorded; reproduce with ``--randomly-seed=<n>``)

Load from a suite's ``conftest.py``, one of two ways::

    pytest_plugins = ("alx.verify.evidence",)  # when the conftest does not import this module

    from alx.verify import evidence  # when it does (for run_dir, git_head):


    def pytest_configure(config):  # register the imported module; loading it by
        config.pluginmanager.register(evidence, "alx.verify.evidence")  # name = rewrite warning

Nothing here touches hardware or firmware; host suites and bench suites use it alike.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    """Return the short HEAD of the repo at ``path``, ``"?"`` when not a repo or git is missing."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):  # git missing, or not a repository
        return "?"


def run_dir(test_dir: str | Path, env_var: str = "ALX_HIL_RUN_DIR") -> Path:
    """Return the per-run output directory: ``ALX_HIL_RUN_DIR`` if set, else a timestamped one.

    Default ``<test_dir>/build/runs/<yymmddHHMMSS>/``; never overwritten, one directory per run. The
    directory is only named here; the session owner creates it.
    """
    env = os.environ.get(env_var)
    return Path(env) if env else Path(test_dir) / "build" / "runs" / time.strftime("%y%m%d%H%M%S")
