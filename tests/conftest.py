# SPDX-License-Identifier: MIT
"""Offline test harness of the Auralix Python Library.

Loads ``alx.verify.evidence`` as a pytest plugin the way a device conftest does, so its collection hook (proof token to
junit property) is exercised by this very suite. No instrument, no target: every module is tested over a fake
transport defined in its test file.
"""

pytest_plugins = ("alx.verify.evidence",)
