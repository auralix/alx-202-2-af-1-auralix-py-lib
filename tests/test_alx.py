# SPDX-License-Identifier: MIT
"""alx - the package itself.

Proofs (ALX-1544):
  P116 alx.__version__ is the version pyproject.toml declares (the two never drift apart; a mutation of
       either is noticed)
"""

import re
from pathlib import Path

import alx


def test_ALX1544_P116_package_version_matches_pyproject():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert match is not None
    assert alx.__version__ == match.group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", alx.__version__)
