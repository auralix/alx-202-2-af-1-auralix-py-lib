# Auralix Python Library - Test

The offline test suite of the library (no instrument, no target: every module is tested over a fake transport)
and the verification lanes around it. The same stages as the Auralix C Library (its `Test/README.md`), with
Python's tools.

This file holds rules and facts only; task details belong to the Jira task and its Task folder notes.

## Run

```
uv sync --locked --extra dev   # once: the environment of every lane (.venv, from uv.lock)
python -m pytest               # dev loop: the offline suite (no instrument, no target, any machine)
uv run nox -l                  # the lanes = the pipeline stages
uv run nox                     # build, test, analyze, sanitize, coverage
uv run nox -s mutate           # report-only, slow
uv run nox -s matrix           # every supported interpreter
uv run nox -s test -- -k cli   # arguments after -- go to pytest
```

The lanes run in the repository's own environment, no second environment per lane; only `matrix`
keeps one venv per interpreter under `.nox/` (`nox --no-reuse` rebuilds them). A missing tool fails
its lane, never skips it.

## Layout

- Library = package `alx/`, grouped by family: a sub-package when a family has a facade plus adapters
  or two related modules, a plain module for a single tool. Public API of a package in its `__init__.py`.
  - `alx.debug_probe`: facade `open()`, contracts `DebugProbe` and `MemoryAccess`, adapter `jlink`
  - `alx.psu`: instrument drivers (`owon_p4603`); a facade follows with the second model
  - `alx.c_lib`: clients of the Auralix C Library protocols - `cli` (framed JSON CLI over a serial
    port, keeps trace bytes aside), `trace` (boot banner and `[ts] [LVL] text` line parsers)
  - `alx.fw`: the firmware image side - `live_watch` (variables by name, read and written through a
    probe while the core runs)
  - `alx.verify`: the pipeline's shared pieces for any repo - `lanes` (the noxfile vocabulary: stage
    names, evidence folders, report options, tool locations), `evidence` (the pytest plugin: proof
    properties, `run_dir`, `git_head`) and the lane gates as commands (`ascii_gate`, `readme_gate`,
    `coverage_gate`, `mutation`)
  - `alx.serial_logger` (days-long UART logging, the soak mode), `alx.errors`
- Tests mirror the package: `tests/<package>/test_<module>.py` (a package, so tools can scope rules),
  imports through `pythonpath = "."`, over fakes: a scripted serial port (`owon_p4603`, `cli`,
  `serial_logger`), a scripted `subprocess` (`jlink`, `live_watch`, `mutation`). Real instrument
  behaviour is qualified on a device repo's bench, never here.
- `noxfile.py` (root) = the lane runner: one nox session per pipeline stage, named after it, running
  inside the repository's uv environment (`uv run nox -s <stage>`). The vocabulary the noxfile of every
  repository shares (stage names, evidence folders, report options, tool locations) is `alx/verify/lanes.py`.
- `build/` = evidence: root = dev lane (`pytest_report.*`); one subfolder per lane, named after the
  stage (`analyze/`, `sanitize/`, `coverage/`, `mutate/`, `matrix/py<ver>/`) and BUILD's product folder
  `dist/` - the C library's layout.
- Naming across the Auralix repositories: folders follow the repository's convention (`tests/`,
  `alx/verify/` here; `Test/`, `Test/Verify/` in the C library; `Tests/`, `Tests/Verify/` in the C# lib);
  files follow the convention of their language wherever they live; the same WORD names the same role
  everywhere, only the casing changes. Lane names are the stage words (`build test analyze sanitize
  coverage mutate`) in every repository, and so are the evidence folders `build/<stage>/`.
- Module naming: abbreviate what the domain abbreviates (psu, fw, cli, mcu, uart, can), spell out common
  Python words (errors, evidence, verify, debug), never shadow the stdlib (test, io, time, serial); a
  module is named after its class in snake_case, instrument modules as manufacturer_model.

## Verification Pipeline

#### WRITE
- **Tools**
	- any editor with ruff (format on save) and a PEP 484 checker (Pylance or mypy)
	- uv -> `uv sync --locked --extra dev`
- **Files - Config**
	- `pyproject.toml` (project, ruff, codespell, mypy, coverage, pytest)
	- `.editorconfig`, `.gitattributes`, `.gitignore`
	- `uv.lock`
- **Files - Generated**
	- `.venv/` (uv, the environment of every lane), `.nox/` (matrix only: one environment per interpreter)

#### BUILD - HOST
- **Tools**
	- `python -m compileall` (byte-compile = syntax over the whole package)
	- `uv build` (PEP 517 sdist + wheel), twine `check --strict` (metadata), check-wheel-contents
	- uv venv + install of the wheel + import of every module = install smoke
- **Files - Config**
	- `pyproject.toml` -> `[build-system]`, `[project]`, `[tool.setuptools]`
- **Files - Code**
	- `noxfile.py` -> `build`
- **Files - Generated**
	- `build/dist/*.whl`, `build/dist/*.tar.gz`, `build/dist/smoke/`

#### TEST - HOST
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
	- `noxfile.py` -> `test`, `matrix`
- **Files - Generated**
	- `build/pytest_report.xml`, `build/pytest_report.html`
	- `build/matrix/py<ver>/` (the same pair per interpreter) -> `build/matrix/py312/`

#### ANALYZE
- **Tools**
	- ruff format --check + ruff check (PEP 8, PEP 257, pyupgrade, bugbear, bandit, pytest style, pathlib, ...) + codespell + `alx.verify.ascii_gate` + `alx.verify.readme_gate` (`--exclude` for vendor folders in other repos) -> Stage 0
	- mypy `--strict` (PEP 484; tests checked, untyped defs allowed there) -> Stage 1
	- pip-audit over `uv export` of `uv.lock` (known vulnerabilities of the locked dependencies) -> Stage 2
- **Files - Config**
	- `pyproject.toml` -> `[tool.ruff]`, `[tool.codespell]`, `[tool.mypy]`
- **Files - Code**
	- `noxfile.py` -> `analyze`
	- `alx/verify/ascii_gate.py`, `alx/verify/readme_gate.py`
- **Files - Generated**
	- `build/analyze/ruff.xml`, `mypy.xml`, `ascii_gate.txt`, `readme_gate.txt`, `requirements.txt`, `pip_audit.json`

#### SANITIZE
- **Tools**
	- CPython development mode: `python -X dev -X faulthandler -X tracemalloc=5 -W error -m pytest` = the same suite with the debug memory allocator, ResourceWarning and unraisable-exception detection, every warning an error
- **Files - Code**
	- `noxfile.py` -> `sanitize`
- **Files - Generated**
	- `build/sanitize/pytest_report.xml`, `build/sanitize/pytest_report.html`

#### COVERAGE
- **Tools**
	- coverage.py (branch) through pytest-cov
	- `alx.verify.coverage_gate` (gate): 100 % lines AND branches on every package file; reads cobertura XML, llvm-cov export JSON (`--metrics lines,branches,regions,functions`) and coverage.py JSON, so the C library and the C# lane gate with the same command
- **Files - Config**
	- `pyproject.toml` -> `[tool.coverage.*]`
- **Files - Code**
	- `noxfile.py` -> `coverage`
	- `alx/verify/coverage_gate.py`
- **Files - Generated**
	- `build/coverage/coverage.xml` (cobertura, gate input), `coverage.json`, `html/index.html`, `coverage_gate.txt`, `pytest_report.*`

#### MUTATE
- **Tools**
	- universalmutator (mutant generation, the C library's generator)
	- `alx.verify.mutation`: plant, run the mirror test module, classify, restore; filters = parse check + normalized-AST fingerprint (docstrings, annotations, positions ignored); crash recovery from `build/mutate/backup/`; hooks `--check-cmd`, `--fingerprint-cmd`, `--rebuild-cmd` and `--tests-dir` for compiled languages (the C library)
- **Files - Code**
	- `noxfile.py` -> `mutate`
	- `alx/verify/mutation.py`
- **Files - Generated**
	- `build/mutate/mutants/<module>/`, `survivors/*.diff`, `report.txt`, `results.json`

#### BUILD - TARGET
- not applicable: pure Python, nothing is built for a target. The wheel of BUILD - HOST and the interpreter matrix of TEST - HOST take its place.

#### TEST - TARGET
- the device repos' HIL suites consume this library: their `Test/conftest.py`, `Test/noxfile.py` (session `hil`), `build/runs/<timestamp>/` with `run_info.txt` naming this repo's head

## Stack

- Types: what a module needs from a transport or a probe is a `Protocol` (`cli.Wire`,
  `owon_p4603.Port`, `serial_logger.Port`, `debug_probe.MemoryAccess`); pyserial and the adapters
  satisfy them, so do the fakes. mypy runs strict on the package; tests are checked but need no
  annotations.
- pyserial is the only runtime dependency. `pyproject.toml` makes the package installable
  (`pip install -e .`, extras `test` and `dev`).
- Consumers depend on this library through a uv project in their test folder (`pyproject.toml` +
  `uv.lock` + `.venv`, `uv sync --locked`; their noxfile runs in that environment). The dependency source
  follows the repository's own pin mechanism: a library repository pins a released tag
  (`alx-202-2-af-1-auralix-py-lib @ git+https://github.com/auralix/alx-202-2-af-1-auralix-py-lib@v0.1.0`);
  a device repository carries this repo as a git submodule and points uv at it as an editable path source
  (`[tool.uv.sources]`), so the gitlink stays the pin and library edits are live on the bench. No
  `sys.path` or `pythonpath` entries to this repo anywhere.
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
  properties, lane gates, lane vocabulary). A device repo owns its fixtures, roles, ports, supply policy
  VALUES (the driver checks them but does not know them), MCU name, memory map, watch table, launcher and
  soak evaluation.

## Lanes

- Lane = stage: the session name, the `nox -s` argument and the evidence folder are the stage word,
  the same in every repository (`build test analyze sanitize coverage mutate`; a C or C# repo has the
  same `noxfile.py` in its test folder, importing `alx.verify.lanes`).
- One suite serves every lane: SANITIZE, COVERAGE and MUTATE re-run `tests/` under other conditions;
  a lane never has tests of its own.
- Random order every run (pytest-randomly), never a fixed seed: the seed is recorded as the junit
  testsuite property `randomly_seed` by `alx.verify.evidence`; reproduce a run with
  `--randomly-seed=<n>`. Suites that drive one stateful device run in file order (`-p no:randomly`)
  and record nothing.
- The interpreter matrix (`uv run nox -s matrix`) is on demand locally and routine in CI; it is not in
  the default set.
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
  `@pytest.mark.req("ALX-<key>-P<n>")`; `alx.verify.evidence` mirrors both into junit `<property>` elements.
  Loading it: `pytest_plugins = ("alx.verify.evidence",)` when the conftest does not import it (this suite),
  else register the imported module in `pytest_configure` (a device conftest that also uses `run_dir`).
- A known defect is sealed as `xfail(strict=True)` with the finding as the reason; it XPASSes when
  fixed and the marker is removed in the green commit.
- CHARACTERIZATION tests (docstring prefix) pin behaviour that is not a requirement, so a change is
  noticed. Metric tests record numbers and assert only a sanity bound.
- Sources, tests and documentation are pure ASCII (`alx.verify.ascii_gate`, ANALYZE Stage 0).
- Markdown uses the heading levels `#` (title), `##` (chapter) and `####` (sub-chapter) only, ATX style, and
  no horizontal rules - chapters separate the text (`alx.verify.readme_gate`, ANALYZE Stage 0).
- Every commit: `python -m pytest` exit code 0 and the public-repository gate clean, in that order,
  each as its own command.

## Jira

- https://auralix.atlassian.net/browse/ALX-1544
