# Auralix Python Library - Test

---




# Human Notes

---

# Verification Pipeline

The same stages as the Auralix C Library (its `Test/README.md`), with Python's tools. One runner,
`nox` (`noxfile.py`, one session per stage); evidence under `build/<stage>/`, the dev lane under
`build/` itself.

## WRITE
- **Tools**
	- any editor with ruff (format on save) and a PEP 484 checker (Pylance or mypy)
	- uv -> `uv sync --locked --extra dev`
- **Files - Config**
	- `pyproject.toml` (project, ruff, codespell, mypy, coverage, pytest)
	- `.editorconfig`, `.gitattributes`, `.gitignore`
	- `uv.lock`
- **Files - Generated**
	- `.venv/` (uv), `.nox/` (one environment per lane)

## BUILD - HOST
- **Tools**
	- `python -m compileall` (byte-compile = syntax over the whole package)
	- build (PEP 517 sdist + wheel), twine `check --strict` (metadata), check-wheel-contents
	- uv venv + install of the wheel + import of every module = install smoke
- **Files - Config**
	- `pyproject.toml` -> `[build-system]`, `[project]`, `[tool.setuptools]`
- **Files - Code**
	- `noxfile.py` -> `build`
- **Files - Generated**
	- `build/dist/*.whl`, `build/dist/*.tar.gz`, `build/dist/smoke/`

## TEST - HOST
- **Tools**
	- python >= 3.10; matrix 3.10 .. 3.13 (uv provides the interpreters)
	- pytest + plugins: pytest-html, pytest-timeout, pytest-randomly
	- hypothesis (property tests of parsers and codecs)
- **Files - Config**
	- `pyproject.toml` -> `[tool.pytest.ini_options]`
- **Files - Code**
	- `tests/conftest.py`
	- `tests/<package>/test_<module>.py` -> `tests/c_lib/test_cli.py`
	- `Fake<Thing>` classes inside the test file -> `FakeWire`
	- `noxfile.py` -> `tests`, `matrix`
- **Files - Generated**
	- `build/pytest_report.xml`, `build/pytest_report.html`
	- `build/matrix/py<ver>/` (the same pair per interpreter) -> `build/matrix/py312/`

## ANALYZE
- **Tools**
	- ruff format --check + ruff check (PEP 8, PEP 257, pyupgrade, bugbear, bandit, pytest style, pathlib, ...) + codespell + `alx.verify.ascii_gate` -> Stage 0
	- mypy `--strict` (PEP 484; tests checked, untyped defs allowed there) -> Stage 1
	- pip-audit (known vulnerabilities of the locked dependencies) -> Stage 2
- **Files - Config**
	- `pyproject.toml` -> `[tool.ruff]`, `[tool.codespell]`, `[tool.mypy]`
- **Files - Code**
	- `noxfile.py` -> `analyze`
	- `alx/verify/ascii_gate.py`
- **Files - Generated**
	- `build/analysis/ruff.xml`, `mypy.xml`, `ascii_gate.txt`, `pip_audit.json`

## SANITIZE
- **Tools**
	- CPython development mode: `python -X dev -X faulthandler -X tracemalloc=5 -W error -m pytest` = the same suite with the debug memory allocator, ResourceWarning and unraisable-exception detection, every warning an error
- **Files - Code**
	- `noxfile.py` -> `sanitize`
- **Files - Generated**
	- `build/sanitize/pytest_report.xml`, `build/sanitize/pytest_report.html`

## COVERAGE
- **Tools**
	- coverage.py (branch) through pytest-cov
	- `alx.verify.coverage_gate` (gate): 100 % lines AND branches on every package file
- **Files - Config**
	- `pyproject.toml` -> `[tool.coverage.*]`
- **Files - Code**
	- `noxfile.py` -> `coverage`
	- `alx/verify/coverage_gate.py`
- **Files - Generated**
	- `build/cov/coverage.xml` (cobertura), `coverage.json` (gate input), `html/index.html`, `coverage_gate.txt`, `pytest_report.*`

## MUTATE
- **Tools**
	- universalmutator (mutant generation, the C library's generator)
	- `alx.verify.mutation`: plant, run the mirror test module, classify, restore; `py_compile` and bytecode-compare filters
- **Files - Code**
	- `noxfile.py` -> `mutate`
	- `alx/verify/mutation.py`
- **Files - Generated**
	- `build/mutation/mutants/<module>/`, `survivors/*.diff`, `report.txt`, `results.json`

## COMPILE - TARGET
- not applicable: pure Python, nothing is built for a target. The wheel of BUILD - HOST and the interpreter matrix of TEST - HOST take its place.

## TEST - TARGET
- the device repos' HIL suites consume this library: their `Test/conftest.py`, `RunHil.ps1`, `build/runs/<timestamp>/` with `run_info.txt` naming this repo's head



# AI Notes

---

This file holds rules and facts only; task details belong to the Jira task and its Task folder notes.

## Run

```
python -m pytest        # dev loop: the offline suite (no instrument, no target, any machine)
nox -l                  # the lanes
nox                     # analyze, tests, sanitize, coverage, build
nox -s mutate           # report-only, slow
nox -s matrix           # every supported interpreter
nox -s tests -- -k cli  # arguments after -- go to pytest
```

Reproducible environment: `uv sync --locked --extra dev` (`uv.lock`). nox reuses its `.nox/`
environments; `nox --no-reuse` rebuilds them. A missing tool fails its lane, never skips it.

## Stack

- Library = package `alx/`, grouped by family: a sub-package when a family has a facade plus adapters
  or two related modules, a plain module for a single tool. Public API of a package in its `__init__.py`.
  - `alx.debug_probe`: facade `open()`, contracts `DebugProbe` and `MemoryAccess`, adapter `jlink`
  - `alx.psu`: instrument drivers (`owon_p4603`); a facade follows with the second model
  - `alx.c_lib`: clients of the Auralix C Library protocols - `cli` (framed JSON CLI over a serial
    port, keeps trace bytes aside), `trace` (boot banner and `[ts] [LVL] text` line parsers)
  - `alx.fw`: the firmware image side - `live_watch` (variables by name, read and written through a
    probe while the core runs)
  - `alx.testing`: pytest evidence helpers (proof properties, `run_dir`, `git_head`)
  - `alx.verify`: the lane gates as commands (`ascii_gate`, `coverage_gate`, `mutation`), for any repo
  - `alx.serial_logger` (days-long UART logging, the soak mode), `alx.errors`
- Naming: abbreviate what the domain abbreviates (psu, fw, cli, mcu, uart, can), spell out common
  Python words (errors, testing, verify, debug), never shadow the stdlib (test, io, time, serial); a
  module is named after its class in snake_case, instrument modules as manufacturer_model.
- Types: what a module needs from a transport or a probe is a `Protocol` (`cli.Wire`,
  `owon_p4603.Port`, `serial_logger.Port`, `debug_probe.MemoryAccess`); pyserial and the adapters
  satisfy them, so do the fakes. mypy runs strict on the package; tests are checked but need no
  annotations.
- pyserial is the only runtime dependency. `pyproject.toml` makes the package installable
  (`pip install -e .`, extras `test` and `dev`); device repos pin this repo as a git submodule and
  put its root on pytest's `pythonpath` instead.
- Tests mirror the package: `tests/<package>/test_<module>.py` (a package, so tools can scope rules),
  imports through `pythonpath = "."`, over fakes: a scripted serial port (`owon_p4603`, `cli`,
  `serial_logger`), a scripted `subprocess` (`jlink`, `live_watch`, `mutation`). Real instrument
  behaviour is qualified on a device repo's bench, never here.
- Property tests (hypothesis) for every parser and codec: JSON framing under arbitrary chunking, trace
  lines, live-watch formats, Commander `mem8` output. `deadline=None` where the code waits on time; no
  function-scoped fixture inside `@given` (a `tempfile` context instead).
- Vocabulary: an equipment *class* (DebugProbe, Psu, ...) is a facade with fixed method names; a *role*
  (dut_power, input, dut_cli, probe) is what an instance does on one bench and lives in the device
  conftest; an *instance* (model, serial number, port) is machine configuration in environment
  variables (`ALX_HIL_DEBUG_PROBE`, `ALX_HIL_DEBUG_PROBE_SN`, `ALX_HIL_JLINK`, `ALX_HIL_PORT`, ...),
  never in git.
- Boundary: the library holds mechanisms (SCPI query/verify, brace-balanced JSON framing, trace
  parsing, probe scripts, ELF symbol resolve + memory decode, line logging with rotation, proof
  properties, lane gates). A device repo owns its fixtures, roles, ports, supply policy VALUES (the
  driver checks them but does not know them), MCU name, memory map, watch table, launcher and soak
  evaluation.

## Lanes

- `build/` layout: root = dev lane (`pytest_report.*`); one subfolder per lane (`analysis/`,
  `sanitize/`, `cov/`, `mutation/`, `dist/`, `matrix/py<ver>/`) - the C library's layout.
- One suite serves every lane: SANITIZE, COVERAGE and MUTATE re-run `tests/` under other conditions;
  a lane never has tests of its own.
- Style and spelling gates live in ANALYZE Stage 0, not inside pytest: a style finding is not a test
  result and must never count as a killed mutant.
- Coverage gate = 100 % lines and branches on every package file. A line that cannot run on the host
  carries `# pragma: no cover - <why>` (POSIX branches on the Windows bench, `__main__` entries);
  Protocol stubs (`...`) and `TYPE_CHECKING` blocks are excluded by configuration.
- Mutation is report-only: a survivor gets a killing test (proof group "mutation-driven hardening")
  or a note as an equivalent mutant in the task notes; 100 % kill rate is not the target.
- Gates are commands in `alx.verify` (`python -m alx.verify.<gate> ...`), each exit 0 / 1 with a
  written report, so another repository's lanes can call the same gates.

## Conventions

- Proof naming: `test_ALX<key>_P<n>_<behavior>`; the proof token stays forever, later tasks attach
  `@pytest.mark.req("ALX-<key>-P<n>")`; `alx.testing` mirrors both into junit `<property>` elements.
  Loading it: `pytest_plugins = ("alx.testing",)` when the conftest does not import it (this suite),
  else register the imported module in `pytest_configure` (a device conftest that also uses `run_dir`).
- A known defect is sealed as `xfail(strict=True)` with the finding as the reason; it XPASSes when
  fixed and the marker is removed in the green commit.
- CHARACTERIZATION tests (docstring prefix) pin behaviour that is not a requirement, so a change is
  noticed. Metric tests record numbers and assert only a sanity bound.
- Sources, tests and documentation are pure ASCII (`alx.verify.ascii_gate`, Stage 0).
- Every commit: `python -m pytest` exit code 0 and the public-repository gate clean, in that order,
  each as its own command.

## Jira

- https://auralix.atlassian.net/browse/ALX-1544
