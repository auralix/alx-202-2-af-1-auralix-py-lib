# SPDX-License-Identifier: MIT
"""Gates of the verification lanes, usable by any repository (this one, a C library, a device repo).

Each module is a command (``python -m alx.verify.<gate> ...``) and a function; every gate prints its
verdict, writes it to ``--out`` when asked, and exits 0 = PASS / 1 = FAIL. Nothing here knows the
library's own modules: paths, thresholds and commands come from the caller (the lane runner).

* ``ascii_gate``: every text file is pure ASCII.
* ``coverage_gate``: every file of a coverage.py JSON report reaches the minimum (lines, branches).
* ``mutation``: plant each mutant of a source, run its tests, classify, report the survivors.
"""
