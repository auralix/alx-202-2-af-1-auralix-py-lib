# SPDX-License-Identifier: MIT
"""Shared pieces of the verification pipeline, for any repository (py-lib, C library, device repo).

Nothing here knows the library's own modules: paths, thresholds and commands come from the caller
(the lane runner).

* ``lanes``: the lane vocabulary of every repository's ``noxfile.py`` - stage names = session names,
  evidence under ``build/<stage>/``, pytest report options, tool locations from the environment.
* ``evidence``: the pytest plugin every suite loads - proof tokens and ``req`` markers into junit
  properties, the per-run folder, repository heads with their dirty mark, a run's evidence copied
  into a release (this one runs inside the test process).
* ``results``: read pytest JUnit evidence without counting expected failures as passes, and write
  the functionality matrix of a target run.
* ``host_build``: the host build that turns C sources into the DLL a pytest suite drives through
  ctypes (toolchain, rebuild check, compile database, the one-step and two-step recipes, the
  sanitizer and coverage variants). Every C repository with host tests needs it, none owns it.
* ``mutation``: plant each mutant of a source, run its tests, classify, report the survivors;
  ``mutation_hooks``: what the lane must ask a compiler when the mutated language is C - is the
  mutant valid C, is its object code the same, rebuild the binaries under test.
* ``configs``: the configuration matrix of a C repository - every source compiled in every row
  of its matrix file, each row's configuration header synthesized from fragments, every finding,
  the headers and names the fakes lack, and the lines no row compiled.
* ``gates``: the gates, each a command (``python -m alx.verify.gates.<gate> ...``) with a PASS/FAIL
  verdict, ``--out`` and exit code 0 = PASS / 1 = FAIL, and a function: ``ascii``, ``c_style``,
  ``configs``, ``coverage``, ``data_source``, ``fake_style``, ``layout``, ``public``.
"""
