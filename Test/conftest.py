"""Auralix Python Library - offline test harness.

Loads alxHil as a pytest plugin exactly the way a device Test/conftest.py does, so its collection hook
(proof token -> junit property) is exercised by this very suite. No instrument, no target: every module
is tested over a fake transport defined in its test file.
"""

pytest_plugins = ("alxHil",)
