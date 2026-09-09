# Auralix Python Library - Test

---




# Human Notes

---




# AI Notes

---

This file holds rules and facts only; task details belong to the Jira task and its Task folder notes.

## Run

```
python -m pytest          # offline: no instrument, no target, any machine; includes the ruff style gate
```

## Stack

- Library = package `alx/`, grouped by family: a sub-package when a family has a facade plus adapters
  or two related modules, a plain module for a single tool. Public API of a package in its `__init__.py`.
  - `alx.debug_probe`: facade `open()`, contract `DebugProbe`, adapter `jlink`
  - `alx.psu`: instrument drivers (`owon_p4603`); a facade follows with the second model
  - `alx.c_lib`: clients of the Auralix C Library protocols - `cli` (framed JSON CLI over a serial
    port, keeps trace bytes aside), `trace` (boot banner and `[ts] [LVL] text` line parsers)
  - `alx.fw`: the firmware image side - `live_watch` (variables by name, read and written through a
    probe while the core runs)
  - `alx.testing`: pytest evidence helpers (proof properties, `run_dir`, `git_head`)
  - `alx.serial_logger` (days-long UART logging, the soak mode), `alx.errors`
- Naming: abbreviate what the domain abbreviates (psu, fw, cli, mcu, uart, can), spell out common
  Python words (errors, testing, debug), never shadow the stdlib (test, io, time, serial); a module is
  named after its class in snake_case, instrument modules as manufacturer_model. PEP 8 / PEP 257 /
  PEP 484 throughout, enforced by ruff (`pyproject.toml`: line length 100, pep257 docstrings, tests
  exempt from docstring and name rules); `tests/test_ruff.py` makes `ruff check` and
  `ruff format --check` part of the run.
- pyserial is the only runtime dependency. `pyproject.toml` makes the package installable
  (`pip install -e .`); device repos pin this repo as a git submodule and put its root on pytest's
  `pythonpath` instead.
- Tests mirror the package: `tests/<package>/test_<module>.py`, imports through `pythonpath = "."`,
  over fakes: a scripted serial port (`owon_p4603`, `cli`, `serial_logger`), a scripted `subprocess`
  (`jlink`, `live_watch`, `serial_logger` detached lifecycle). Real instrument behaviour is qualified
  on a device repo's bench, never here.
- Proof naming: `test_ALX<key>_P<n>_<behavior>`; the proof token stays forever, later tasks attach
  `@pytest.mark.req("ALX-<key>-P<n>")`; `alx.testing` mirrors both into junit `<property>` elements.
  Loading it: `pytest_plugins = ("alx.testing",)` when the conftest does not import it (this suite),
  else register the imported module in `pytest_configure` (a device conftest that also uses `run_dir`).
  Evidence per run: `build/pytest_report.xml` + `build/pytest_report.html`.
- Vocabulary: an equipment *class* (DebugProbe, Psu, ...) is a facade with fixed method names; a *role*
  (dut_power, input, dut_cli, probe) is what an instance does on one bench and lives in the device
  conftest; an *instance* (model, serial number, port) is machine configuration in environment
  variables (`ALX_HIL_DEBUG_PROBE`, `ALX_HIL_DEBUG_PROBE_SN`, `ALX_HIL_JLINK`, `ALX_HIL_PORT`, ...),
  never in git.
- Boundary: the library holds mechanisms (SCPI query/verify, brace-balanced JSON framing, trace
  parsing, probe scripts, ELF symbol resolve + memory decode, line logging with rotation, proof
  properties). A device repo owns its fixtures, roles, ports, supply policy VALUES (the driver checks
  them but does not know them), MCU name, memory map, watch table, launcher and soak evaluation.

## Jira

- https://auralix.atlassian.net/browse/ALX-1544
