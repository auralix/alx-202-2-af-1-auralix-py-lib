# SPDX-License-Identifier: MIT
"""The gates of the verification pipeline: each a command with a PASS/FAIL verdict and exit code.

``python -m alx.verify.gates.<gate> ...`` prints the verdict, writes it to ``--out`` when asked and
exits 0 = PASS / 1 = FAIL; each is also a function. ``ascii`` (pure ASCII text), ``c_style`` (the
mechanical C rules), ``configs`` (every row of a configuration matrix clean, every line compiled by
some row), ``coverage`` (every file at the minimum), ``data_source`` (every test data file
identifies itself), ``fake_style`` (the fake convention), ``layout`` (the verification template),
``public`` (no private vocabulary in a public repository).
"""
