# SPDX-License-Identifier: MIT
"""The verification runner depends on mechanisms, never its test application.

And the repository is laid out by the verification template, which alx.verify.layout states as
data: the same check every repository's own architecture self-test calls on itself.
"""

import subprocess
import sys
from pathlib import Path

from alx.verify import layout


def test_ALX1564_P337_this_repository_follows_the_verification_template():
    assert layout.check(Path(__file__).resolve().parents[2], "python", ["alx"]) == []


def test_ALX1564_P203_runner_import_does_not_load_the_test_application():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import noxfile; "
            "assert 'conftest' not in sys.modules; "
            "assert not any(name == 'tests' or name.startswith('tests.') for name in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
