#*******************************************************************************
# @file			alxHil.py
# @brief		Auralix Python Library - HIL pytest Module
# @copyright	Copyright (C) Auralix d.o.o. All rights reserved.
#*******************************************************************************

"""pytest side of a bench (HIL) suite - the parts every device repo needs and none should own:

  - traceability hook: the proof token in a test's NAME (test_ALX<key>_P<n>_...) is the primary trace
    link; it is mirrored into the junit XML as <property name="proof">, and every
    @pytest.mark.req("ALX-<key>-P<n>") marker as <property name="req"> (junit_family = xunit1)
  - parse_banner(): the identity the Auralix C Library firmware traces on its debug UART after reset
  - git_head(): short HEAD of a repo (fw repo, submodules) for the run's identity record
  - run_dir(): the per-run evidence directory (ALX_HIL_RUN_DIR from the launcher, else a timestamp)

Load from the device's Test/conftest.py, one of two ways:

    pytest_plugins = ("alxHil",)                    # when that conftest does NOT import alxHil itself

    import alxHil                                   # when it does (for parse_banner, run_dir, ...):
    def pytest_configure(config):                   # register the imported module - loading it by name
        config.pluginmanager.register(alxHil, "alxHil")   # afterwards would raise the assert-rewrite warning

Nothing here touches hardware; the fixtures (which instrument on which port, under which policy) stay
in the device repo.
"""

import os
import re
import subprocess
import time
from pathlib import Path

PROOF_RE = re.compile(r"ALX(\d+)_P(\d+)")


def pytest_collection_modifyitems(items):
    for item in items:
        m = PROOF_RE.search(item.name)
        if m:
            item.user_properties.append(("proof", f"ALX-{m.group(1)}-P{m.group(2)}"))
        for mark in item.iter_markers(name="req"):
            for rid in mark.args:
                item.user_properties.append(("req", rid))


# --------------------------------------------------------- boot banner ----
BANNER_RE = re.compile(rb"FW Started:.*?- FW Name: (?P<name>\S+).*?- FW Version: (?P<ver>\S+).*?- FW Bin: (?P<bin>\S+\.bin)", re.S)


def parse_banner(raw: bytes) -> dict:
    """The firmware traces its identity on the debug UART right after reset:
    '- FW Name: X', '- FW Version: <maj.min.patch.date.fullhash>', '- FW Bin: <date>_..._V<maj>-<min>-<patch>_<hash7>.bin'.
    Returns {} when no banner is in raw."""
    m = BANNER_RE.search(raw)
    if not m:
        return {}
    d = {k: v.decode("ascii", "replace") for k, v in m.groupdict().items()}
    d["hash7"] = d["bin"].rsplit("_", 1)[-1].removesuffix(".bin")
    return d


# ------------------------------------------------------------ identity ----
def git_head(path) -> str:
    """Short HEAD of the repo at path, '?' when it is not a repo or git is unavailable."""
    try:
        return subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # pragma: no cover
        return "?"


def run_dir(test_dir, env_var: str = "ALX_HIL_RUN_DIR") -> Path:
    """Per-run output directory: the launcher's ALX_HIL_RUN_DIR (junit/html/uart.log land together),
    else <test_dir>/build/runs/<yymmddHHMMSS>/. Never overwritten: one directory per run."""
    env = os.environ.get(env_var)
    return Path(env) if env else Path(test_dir) / "build" / "runs" / time.strftime("%y%m%d%H%M%S")
