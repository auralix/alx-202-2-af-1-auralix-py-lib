# SPDX-License-Identifier: MIT
"""Test harness of the Auralix Python Library; offline unless explicitly requested.

Loads ``alx.verify.evidence`` as a pytest plugin the way a device conftest does, so its collection hook (proof token to
junit property) is exercised by this very suite. Instrument tests require their explicit live option.
"""

import pytest

pytest_plugins = (
    "alx.verify.evidence",
    "pytester",
)  # pytester: run a nested pytest to test the plugin itself


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keysight-live", action="store_true", help="Run the unloaded E36313A output test"
    )
    parser.addoption("--keysight-host", help="LAN host for the explicitly enabled E36313A test")
