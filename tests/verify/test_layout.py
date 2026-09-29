# SPDX-License-Identifier: MIT
"""alx.verify.layout: the verification template as an enforced rule.

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
  P335 test modules are test_<snake_case>.py, test data is not in tests/, every test folder is a
       package
  P336 check() reads what Git tracks, untracked build output is never a finding; main() exits 0 on
       PASS, 1 on FAIL, and --out writes the same report
"""

import subprocess

from alx.verify import layout

C_ROOT = [
    ".gitignore",
    "noxfile.py",
    "pyproject.toml",
    "uv.lock",
    "config/alxConfig.h",
    "data/cli_items.json",
    "harness/__init__.py",
    "harness/build.py",
    "host/checks/alxFifoSanSmoke.c",
    "host/fakes/alxAdcFake.c",
    "host/helpers/alxAssertPc.c",
    "target/checks/boot.c",
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/framework/__init__.py",
    "tests/framework/test_verification_architecture.py",
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
    "alx/verify/layout.py",
    "alx/c_lib/__init__.py",
    "tests/__init__.py",
    "tests/verify/__init__.py",
    "tests/verify/test_layout.py",
    "tests/c_lib/__init__.py",
    "tests/c_lib/test_cli.py",
]


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
        "host/alxLooseFake.c: host/ holds only checks, fakes, helpers/",
        "host/exports/alxFifoTest.def: host/ holds only checks, fakes, helpers/",
        "target/boot.c: target/ holds only checks, helpers/",
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
    py_paths = [*PY_ROOT, "tests/framework/__init__.py", "tests/host/__init__.py"]
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
        "tests/host/alx/fifo_vectors.json: test data belongs in data/",
        "tests/host/alx/ext/: not a package (no __init__.py)",
    ]


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
