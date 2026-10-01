# SPDX-License-Identifier: MIT
"""alx.verify.gates.layout: the verification template as an enforced rule.

The roots here are lists of tracked paths, as Git would name them; one test builds a real
repository to prove the paths are read from Git and nothing else.

Proofs (ALX-1564):
  P330 a C root and a Python root laid out by the template pass
  P331 a missing runner, project file, lock or tests/ tree is a finding
  P332 a root folder outside the template is a finding; root files, hidden folders and the
       import packages are free
  P333 host/ and target/ hold only their role folders, and nothing loose
  P334 the first level of tests/: the execution locations of a C root, the mirrored sub-packages
       of a Python root, framework/ and integration/ in both
  P335 test modules are test_<snake_case>.py, test data is not in tests/, every folder holding
       Python is a package
  P336 check() reads what Git tracks, untracked build output is never a finding; main() exits 0 on
       PASS, 1 on FAIL, and --out writes the same report
  P344 test data is in data/ at the verification root, at any depth inside it; a data folder
       inside tests/ is a finding per file, not a missing package. P343, the rule of 0.12.0 that
       put data beside its tests, is withdrawn: TV decided on 2026-09-30 for one root folder
  P353 a role folder holds one shape of file, directly: <prefix><Module>Fake.c,
       <prefix><Module>TestHelpers.c or ..._TestHelpers_<Part>.c, <prefix><Subject>Check.c,
       <prefix>HostShim.c/.h or a vendor header under its own name
  P354 harness/ holds __init__.py and the packages host/ and target/, whose modules are
       <snake_case>.py and never test_*
  P355 a framework test mirrors what it checks: a harness module (continuations allowed), conftest,
       noxfile, a host role or architecture
  P368 the fakes of a vendor tree sit in host/shim/<vendor>/, a lowercase word, as headers under the
       include paths the code names; another folder name, a file that is not a header or a folder
       no include path could name is a finding
"""

import subprocess

from alx.verify.gates import layout

C_ROOT = [
    ".gitignore",
    "noxfile.py",
    "pyproject.toml",
    "uv.lock",
    "config/alxConfig.h",
    "data/cli_items.json",
    "harness/__init__.py",
    "harness/host/__init__.py",
    "harness/host/build.py",
    "host/checks/alxFifoSanSmokeCheck.c",
    "host/fakes/alxAdcFake.c",
    "host/helpers/alxAssertTestHelpers.c",
    "host/helpers/alxIdTestHelpers_DateComp.c",
    "target/checks/alxBootCheck.c",
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/framework/__init__.py",
    "tests/framework/test_architecture.py",
    "tests/framework/test_fakes.py",
    "tests/host/__init__.py",
    "tests/host/alx/__init__.py",
    "tests/host/alx/test_fifo.py",
    "tests/host/alx/test_param_item_kv.py",
    "tests/target/__init__.py",
    "tests/target/test_boot.py",
]
PY_ROOT = [
    "README.md",
    "noxfile.py",
    "pyproject.toml",
    "uv.lock",
    "alx/__init__.py",
    "alx/verify/__init__.py",
    "alx/verify/gates/__init__.py",
    "alx/verify/gates/layout.py",
    "alx/c_lib/__init__.py",
    "tests/__init__.py",
    "tests/framework/__init__.py",
    "tests/framework/test_architecture.py",
    "tests/verify/__init__.py",
    "tests/verify/gates/__init__.py",
    "tests/verify/gates/test_layout.py",
    "tests/c_lib/__init__.py",
    "tests/c_lib/test_cli.py",
]
ROLES = "host/ holds only checks, fakes, helpers, shim/"


def test_ALX1564_P330_roots_laid_out_by_the_template_pass():
    assert layout.check_paths(C_ROOT, "c") == []
    assert layout.check_paths(PY_ROOT, "python", ["alx"]) == []


def test_ALX1564_P331_the_required_entries_are_required():
    assert layout.check_paths(["README.md"], "c") == [
        "noxfile.py: missing",
        "pyproject.toml: missing",
        "uv.lock: missing",
        "tests/: missing",
    ]


def test_ALX1564_P332_a_root_folder_outside_the_template_is_a_finding():
    paths = [*C_ROOT, "native/fakes/alxAdcFake.c", ".github/workflows/lanes.yml", "AGENTS.md"]
    assert layout.check_paths(paths, "c") == [
        "native/fakes/alxAdcFake.c: native/ is not a folder of the template"
    ]
    assert layout.check_paths([*PY_ROOT, "src/alx/x.py"], "python", ["alx"]) == [
        "src/alx/x.py: src/ is not a folder of the template"
    ]


def test_ALX1564_P333_host_and_target_hold_only_their_role_folders():
    paths = [*C_ROOT, "host/alxLooseFake.c", "host/exports/alxFifoTest.def", "target/boot.c"]
    assert layout.check_paths(paths, "c") == [
        f"host/alxLooseFake.c: {ROLES}",
        f"host/exports/alxFifoTest.def: {ROLES}",
        f"target/boot.c: {ROLES.replace('host/', 'target/')}",
    ]


def test_ALX1564_P334_the_first_level_of_tests_follows_the_repository_kind():
    c_paths = [
        *C_ROOT,
        "tests/integration/__init__.py",
        "tests/unit/__init__.py",
        "tests/unit/test_x.py",
    ]
    assert layout.check_paths(c_paths, "c") == [
        "tests/unit/__init__.py: tests/unit/ is not a folder of the template",
        "tests/unit/test_x.py: tests/unit/ is not a folder of the template",
    ]
    py_paths = [*PY_ROOT, "tests/host/__init__.py"]
    assert layout.check_paths(py_paths, "python", ["alx"]) == [
        "tests/host/__init__.py: tests/host/ is not a folder of the template"
    ], "a Python root mirrors its sub-packages; it has no execution-location level"


def test_ALX1564_P335_test_modules_data_and_packages():
    paths = [
        *C_ROOT,
        "tests/host/alx/TestFifo.py",
        "tests/host/alx/fifo_vectors.json",
        "tests/host/alx/ext/test_ina228.py",
    ]
    assert layout.check_paths(paths, "c") == [
        "tests/host/alx/TestFifo.py: a test module is test_<snake_case>.py",
        "tests/host/alx/fifo_vectors.json: test data belongs in data/ at the verification root",
        "tests/host/alx/ext/: not a package (no __init__.py)",
    ]


def test_ALX1564_P344_test_data_is_in_data_at_the_root_only():
    paths = [
        *C_ROOT,
        "data/tables/crc.csv",
        "tests/host/alx/data/fifo_vectors.json",
        "tests/data/shared.bin",
    ]
    assert layout.check_paths(paths, "c") == [
        "tests/host/alx/data/fifo_vectors.json: test data belongs in data/ at the verification root",
        "tests/data/shared.bin: tests/data/ is not a folder of the template",
    ], "one data/ at the root, any depth inside it; a data folder in tests/ is a finding per file"


def test_ALX1564_P353_a_role_folder_holds_one_shape_of_file_directly():
    paths = [
        *C_ROOT,
        "host/fakes/alxAdcFakes.c",
        "host/fakes/alx_adc_fake.c",
        "host/fakes/sub/alxDacFake.c",
        "host/helpers/alxAssertPc.c",
        "host/helpers/alxIdTestHelpers_dateComp.c",
        "host/checks/alxFifoSanSmoke.c",
        "host/shim/fooHostShim.c",
        "host/shim/fooHostShim.h",
        "host/shim/core_cm4.h",
        "host/shim/fooHostCore.c",
    ]
    fakes = "host/fakes/ holds only <prefix><Module>Fake.c, directly"
    helpers = "host/helpers/ holds only <prefix><Module>TestHelpers.c or ..._TestHelpers_<Part>.c, directly"
    shim = (
        "host/shim/ holds only <prefix>HostShim.c or .h, or the vendor header it replaces, directly"
    )
    assert layout.check_paths(paths, "c") == [
        f"host/fakes/alxAdcFakes.c: {fakes}",
        f"host/fakes/alx_adc_fake.c: {fakes}",
        f"host/fakes/sub/alxDacFake.c: {fakes}",
        f"host/helpers/alxAssertPc.c: {helpers}",
        f"host/helpers/alxIdTestHelpers_dateComp.c: {helpers}",
        "host/checks/alxFifoSanSmoke.c: host/checks/ holds only <prefix><Subject>Check.c, directly",
        f"host/shim/fooHostCore.c: {shim}",
    ], "the shim pair and a vendor header under its own name pass"


def test_ALX1564_P354_harness_holds_two_packages_of_snake_case_modules():
    paths = [
        *C_ROOT,
        "harness/build.py",
        "harness/bench/__init__.py",
        "harness/host/deep/x.py",
        "harness/host/test_build.py",
        "harness/host/Build.py",
        "harness/target/__init__.py",
        "harness/target/flash.py",
    ]
    shape = "harness/ holds __init__.py and the packages host, target/"
    module = "a harness module is <snake_case>.py, never test_*"
    assert layout.check_paths(paths, "c") == [
        f"harness/build.py: {shape}",
        f"harness/bench/__init__.py: {shape}",
        f"harness/host/deep/x.py: {shape}",
        f"harness/host/test_build.py: {module}",
        f"harness/host/Build.py: {module}",
    ], "harness/target/flash.py passes; a module directly in harness/ does not"


def test_ALX1564_P355_a_framework_test_mirrors_what_it_checks():
    paths = [
        *C_ROOT,
        "tests/framework/test_build.py",
        "tests/framework/test_build_groups.py",
        "tests/framework/test_conftest.py",
        "tests/framework/test_noxfile.py",
        "tests/framework/test_fixture_lifecycle.py",
        "tests/framework/test_report_hil.py",
    ]
    mirror = (
        "a framework test mirrors a harness module, conftest, noxfile, a host role or architecture"
    )
    assert layout.check_paths(paths, "c") == [
        f"tests/framework/test_fixture_lifecycle.py: {mirror}",
        f"tests/framework/test_report_hil.py: {mirror}",
    ], "build (a harness module), its continuation, conftest, noxfile, fakes and architecture pass"


def test_ALX1564_P336_check_reads_git_and_main_reports(tmp_path, capsys):
    def git(*args):
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(tmp_path), *args],  # noqa: S607 - the git on PATH is the developer's own
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    for path in C_ROOT:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("x\n", encoding="ascii")
    git("add", ".")
    (tmp_path / "build" / "junk").mkdir(parents=True)
    (tmp_path / "build" / "junk" / "loose.obj").write_text("x\n", encoding="ascii")
    (tmp_path / "native").mkdir()
    (tmp_path / "native" / "untracked.c").write_text("x\n", encoding="ascii")
    assert layout.check(tmp_path, "c") == [], "untracked files are not the repository's"

    assert layout.main([str(tmp_path), "--kind", "c"]) == 0
    assert capsys.readouterr().out == "LAYOUT GATE: PASS\n"

    git("add", "native/untracked.c")
    out = tmp_path / "report" / "layout.txt"
    assert layout.main([str(tmp_path), "--kind", "c", "--out", str(out)]) == 1
    report = capsys.readouterr().out
    assert report == (
        "LAYOUT GATE: FAIL (1 finding(s))\n"
        "native/untracked.c: native/ is not a folder of the template\n"
    )
    assert out.read_text(encoding="ascii") == report


def test_ALX1564_P368_a_vendor_folder_of_the_shim_holds_headers_under_their_include_paths():
    good = [
        *C_ROOT,
        "host/shim/zephyr/zephyr/kernel.h",
        "host/shim/zephyr/zephyr/drivers/gpio.h",
        "host/shim/stm32-usb-host/usbh_core.h",
        "host/shim/mcuboot/flash_map_backend/flash_map_backend.h",
    ]
    assert layout.check_paths(good, "c") == []
    shape = "host/shim/<vendor>/ is a lowercase word holding the vendor's headers (.h) under their include paths"
    bad = [
        *C_ROOT,
        "host/shim/Zephyr/kernel.h",
        "host/shim/zephyr/kernel.c",
        "host/shim/zephyr/my dir/x.h",
    ]
    assert layout.check_paths(bad, "c") == [
        f"host/shim/Zephyr/kernel.h: {shape}",
        f"host/shim/zephyr/kernel.c: {shape}",
        f"host/shim/zephyr/my dir/x.h: {shape}",
    ]
