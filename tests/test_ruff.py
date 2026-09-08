"""Style gate: the library is PEP 8 / PEP 257 / PEP 484 as configured in pyproject.toml, checked by ruff.

Proofs (ALX-1544):
  P99 `ruff check` and `ruff format --check` over the repo report nothing
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ALX1544_P99_ruff_check_and_format_are_clean():
    for args in (["check", "."], ["format", "--check", "."]):
        result = subprocess.run(
            [sys.executable, "-m", "ruff", *args], cwd=ROOT, capture_output=True, text=True
        )
        assert result.returncode == 0, f"ruff {' '.join(args)}:\n{result.stdout}\n{result.stderr}"
