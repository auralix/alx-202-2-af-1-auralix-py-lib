# SPDX-License-Identifier: MIT
"""Shared pieces of the verification pipeline, for any repository (py-lib, C library, device repo).

The gates are commands (``python -m alx.verify.<gate> ...``) and functions; every gate prints its
verdict, writes it to ``--out`` when asked, and exits 0 = PASS / 1 = FAIL. Nothing here knows the
library's own modules: paths, thresholds and commands come from the caller (the lane runner).

* ``lanes``: the lane vocabulary of every repository's ``noxfile.py`` - stage names = session names,
  evidence under ``build/<stage>/``, pytest report options, tool locations from the environment.
* ``evidence``: the pytest plugin every suite loads - proof tokens and ``req`` markers into junit
  properties, the per-run folder, repository heads (this one runs inside the test process).
* ``ascii_gate``: every text file is pure ASCII.
* ``readme_gate``: every Markdown file uses the heading levels ``#``, ``##``, ``####`` only, and no
  horizontal rules.
* ``coverage_gate``: every file of a coverage report (cobertura, llvm-cov, coverage.py) reaches the
  minimum in every gated metric.
* ``mutation``: plant each mutant of a source, run its tests, classify, report the survivors.
"""
