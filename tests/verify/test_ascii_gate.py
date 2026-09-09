# SPDX-License-Identifier: MIT
"""alx.verify.ascii_gate - the pure-ASCII gate over a scratch tree.

Proofs (ALX-1544):
  P100 text files are selected by suffix or by name; tool and build folders are skipped
  P101 the first non-ASCII byte is reported with its line; pure ASCII gives None
  P102 main() prints and writes the report: PASS exits 0, FAIL exits 1 and names the offender once
"""

from pathlib import Path

from alx.verify import ascii_gate


def tree(root: Path) -> None:
    (root / "alx").mkdir()
    (root / "alx" / "mod.py").write_bytes(b"x = 1\n")
    (root / "LICENSE").write_bytes(b"MIT\n")
    (root / "notes.md").write_bytes(b"# ok\n")
    (root / "image.bin").write_bytes(b"\xff\xfe")
    (root / ".venv").mkdir()
    (root / ".venv" / "site.py").write_bytes(b"caf\xc3\xa9\n")
    (root / "build").mkdir()
    (root / "build" / "report.txt").write_bytes(b"\xe2\x9c\x93\n")


def test_ALX1544_P100_text_files_by_suffix_or_name_skipping_tool_folders(tmp_path):
    tree(tmp_path)
    names = [p.relative_to(tmp_path).as_posix() for p in ascii_gate.text_files(tmp_path)]
    assert names == ["LICENSE", "alx/mod.py", "notes.md"]


def test_ALX1544_P101_first_non_ascii_reports_line_and_byte():
    assert ascii_gate.first_non_ascii(b"plain\nascii\n") is None
    assert ascii_gate.first_non_ascii(b"line one\nline two \xe2\x80\x93 dash\n") == (2, 0xE2)
    assert ascii_gate.first_non_ascii(b"\x80") == (1, 0x80)


def test_ALX1544_P102_main_pass_and_fail_with_report_file(tmp_path, capsys):
    tree(tmp_path)
    out = tmp_path / "build" / "analysis" / "ascii_gate.txt"
    assert ascii_gate.main([str(tmp_path)]) == 0
    assert ascii_gate.main([str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text(encoding="ascii").startswith("ASCII GATE: PASS (3 files)")
    assert "PASS" in capsys.readouterr().out

    (tmp_path / "notes.md").write_bytes(b"# ok\n\xe2\x80\x94 not ok\n\xe2\x80\x94 again\n")
    assert ascii_gate.main([str(tmp_path), "--out", str(out)]) == 1
    report = out.read_text(encoding="ascii").splitlines()
    assert report[0] == "ASCII GATE: FAIL (3 files)"
    assert len(report) == 2, "first offender per file only"
    assert report[1].endswith("notes.md:2: byte 0xE2")


def test_ALX1544_P129_gate_scope_covers_the_repository_text_kinds():
    """Mutation-driven hardening: the scope sets name every kind the pipeline writes."""
    assert {
        ".py",
        ".toml",
        ".md",
        ".txt",
        ".json",
        ".yml",
        ".yaml",
        ".ps1",
        ".c",
        ".h",
        ".xml",
        ".csv",
    } <= (ascii_gate.TEXT_SUFFIXES)
    assert {"LICENSE", ".gitignore", ".gitattributes", ".editorconfig", ".python-version"} <= (
        ascii_gate.TEXT_NAMES
    )
    assert {
        ".git",
        ".venv",
        ".nox",
        "build",
        "dist",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
    } <= (ascii_gate.SKIP_DIRS)
    assert {".hypothesis", ".pytest_cache"} <= ascii_gate.SKIP_DIRS
    assert "" not in ascii_gate.TEXT_SUFFIXES | ascii_gate.TEXT_NAMES | ascii_gate.SKIP_DIRS
