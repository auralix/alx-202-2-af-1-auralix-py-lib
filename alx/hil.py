"""The pytest side of a bench (HIL) suite: the parts every device repo needs and none should own.

* traceability hook: the proof token in a test's name (``test_ALX<key>_P<n>_...``) is the primary
  trace link; it is mirrored into the junit XML as ``<property name="proof">``, every
  ``@pytest.mark.req("ALX-<key>-P<n>")`` marker as ``<property name="req">`` (``junit_family =
  xunit1``)
* ``parse_banner``: the identity the Auralix C Library firmware traces on its debug UART after reset
* ``git_head``: short HEAD of a repo (fw repo, submodules) for the run's identity record
* ``run_dir``: the per-run evidence directory (``ALX_HIL_RUN_DIR`` from the launcher, else a
  timestamp)

Load from a device ``Test/conftest.py`` in one of two ways::

    pytest_plugins = ("alx.hil",)               # when the conftest does not import alx.hil itself

    from alx import hil                         # when it does (for parse_banner, run_dir, ...):
    def pytest_configure(config):               # register the imported module; loading it by
        config.pluginmanager.register(hil, "alx.hil")  # name afterwards = assert-rewrite warning

Nothing here touches hardware; the fixtures (which instrument on which port, under which policy)
stay in the device repo.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

PROOF_RE = re.compile(r"ALX(\d+)_P(\d+)")
BANNER_RE = re.compile(
    rb"FW Started:.*?- FW Name: (?P<name>\S+).*?- FW Version: (?P<ver>\S+)"
    rb".*?- FW Bin: (?P<bin>\S+\.bin)",
    re.S,
)


def pytest_collection_modifyitems(items) -> None:
    """Mirror the proof token of every test name and every ``req`` marker into junit properties."""
    for item in items:
        match = PROOF_RE.search(item.name)
        if match:
            item.user_properties.append(("proof", f"ALX-{match.group(1)}-P{match.group(2)}"))
        for mark in item.iter_markers(name="req"):
            for rid in mark.args:
                item.user_properties.append(("req", rid))


def parse_banner(raw: bytes) -> dict:
    """Extract name, version, bin and 7-char build hash from a boot transcript, ``{}`` if none.

    The firmware traces ``- FW Name: X``, ``- FW Version: <maj.min.patch.date.fullhash>`` and
    ``- FW Bin: <date>_..._V<maj>-<min>-<patch>_<hash7>.bin`` right after reset.
    """
    match = BANNER_RE.search(raw)
    if not match:
        return {}
    ident = {k: v.decode("ascii", "replace") for k, v in match.groupdict().items()}
    ident["hash7"] = ident["bin"].rsplit("_", 1)[-1].removesuffix(".bin")
    return ident


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
