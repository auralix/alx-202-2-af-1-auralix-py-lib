# SPDX-License-Identifier: MIT
"""alx.verify.public_gate: the mechanism, driven by a vocabulary invented for these tests.

The real vocabulary is private and lives outside this repository - that is the whole point of the
module - so everything here is fabricated: "Widget" is the forbidden name, "W-1234" the forbidden
pattern, "Vendor" the tree nobody here wrote.

Proofs (ALX-1544):
  P192 a vocabulary loads: groups contribute literals and patterns, excludes come along, and the
       keys are all optional
  P193 the gate FAILS when it has no vocabulary - neither --words nor the environment - because a
       machine that merely lacks the list must never report PASS
  P194 a literal matches inside a longer identifier, which is how these names really appear, and a
       pattern matches what a bare substring would over-match
  P195 excludes: a folder name anywhere, an exact relative path, a path prefix, and no false match
  P196 binary is decided by a NUL byte near the start, not by the file's suffix
  P197 a non-ASCII line is a finding, and the line quoted back stays ASCII whatever the file held
  P198 the tree scan skips tool folders, generated names and excluded trees, and says what it read
  P199 commit messages of base..HEAD are scanned; a base that does not resolve is reported, not
       silently passed
  P200 --history walks every commit, over full diffs or with --diff-only only the added lines
  P201 main(): 0 on PASS, 1 on FAIL, --out writes the same report, --commits-only skips the tree
  P202 main() with no vocabulary reports FAIL as a verdict, not a traceback, and writes that verdict
       to --out like any other - a caller reading only the first line still learns the gate did not run
"""

import json
import subprocess
from pathlib import Path

import pytest

from alx.verify import public_gate
from alx.verify.public_gate import Vocabulary

VOCAB = {
    "groups": [
        {"why": "the product nobody may name", "literals": ["Widget"]},
        {"why": "its build ids", "patterns": [r"\bW-[0-9]+"]},
    ],
    "excludes": ["Vendor"],
    "decided_public": [{"what": "Gadget", "when": "2026-01-01", "why": "released to the world"}],
}


@pytest.fixture
def vocab_file(tmp_path):
    path = tmp_path / "vocab.json"
    path.write_text(json.dumps(VOCAB), encoding="ascii")
    return path


def _git(root, *args):
    # the fixture repository this test builds for itself; argv is these constants and a tmp_path
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(root), *args],  # noqa: S607 - the git on PATH is the developer's own
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A real repository: one clean commit tagged `base`, then one that leaks in file and message."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    _git(root, "config", "user.email", "tester@example.invalid")
    _git(root, "config", "user.name", "Tester")
    (root / "clean.txt").write_text("nothing to see here\n", encoding="ascii")
    _git(root, "add", "clean.txt")
    _git(root, "commit", "-q", "-m", "first commit, nothing to see")
    _git(root, "tag", "base")
    (root / "leak.c").write_text("void WidgetBringUp(void) {}\n", encoding="ascii")
    _git(root, "add", "leak.c")
    _git(root, "commit", "-q", "-m", "second commit: wire up W-1234")
    return root


def test_ALX1544_P192_a_vocabulary_loads_and_every_key_is_optional(tmp_path, vocab_file):
    vocab = public_gate.load(vocab_file)
    assert vocab.literals == ("Widget",)
    assert [p.pattern for p in vocab.patterns] == [r"\bW-[0-9]+"]
    assert vocab.excludes == ("Vendor",)

    bare = tmp_path / "bare.json"
    bare.write_text("{}", encoding="ascii")
    assert public_gate.load(bare) == Vocabulary()

    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"groups": [{"why": "no words at all"}]}), encoding="ascii")
    assert public_gate.load(partial) == Vocabulary()


def test_ALX1544_P193_no_vocabulary_is_a_failure_never_a_silent_pass(tmp_path, monkeypatch):
    monkeypatch.delenv(public_gate.WORDS_ENV, raising=False)
    with pytest.raises(FileNotFoundError, match="never passes without one"):
        public_gate.vocabulary_path()

    missing = tmp_path / "gone.json"
    with pytest.raises(FileNotFoundError, match="vocabulary file not found"):
        public_gate.vocabulary_path(str(missing))

    present = tmp_path / "there.json"
    present.write_text("{}", encoding="ascii")
    assert public_gate.vocabulary_path(str(present)) == present
    monkeypatch.setenv(public_gate.WORDS_ENV, str(present))
    assert public_gate.vocabulary_path() == present
    assert public_gate.vocabulary_path(str(present)) == present


def test_ALX1544_P194_a_literal_matches_inside_an_identifier_and_a_pattern_where_it_cannot(
    vocab_file,
):
    vocab = public_gate.load(vocab_file)
    # the name embedded in a longer token is the case a word boundary would MISS
    assert vocab.hits("f.c", "void WidgetBringUp(void)\n") == [
        "FORBIDDEN 'Widget'         f.c:1: void WidgetBringUp(void)"
    ]
    # the pattern reports what it matched, not the whole entry
    assert vocab.hits("f.c", "see W-1234 for the rest\n") == [
        "FORBIDDEN 'W-1234'         f.c:1: see W-1234 for the rest"
    ]
    # a line may carry both, and the line number is the line it is on
    both = vocab.hits("f.c", "clean\nWidget and W-9\n")
    assert len(both) == 2
    assert all(":2:" in finding for finding in both)
    assert vocab.hits("f.c", "Gadget is fine, W- is not a build id\n") == []


def test_ALX1544_P195_excludes_match_a_folder_name_a_path_and_a_prefix():
    assert public_gate.excluded(Path("Vendor/x.c"), ["Vendor"])
    assert public_gate.excluded(Path("deep/Vendor/x.c"), ["Vendor"])
    assert public_gate.excluded(Path("Doc/gen"), ["Doc/gen"])
    assert public_gate.excluded(Path("Doc/gen/x.md"), ["Doc/gen"])
    assert public_gate.excluded(Path("Doc/gen/x.md"), ["Doc\\gen"])
    assert not public_gate.excluded(Path("Test/x.c"), ["Vendor"])
    assert not public_gate.excluded(Path("Test/x.c"), [])


def test_ALX1544_P196_binary_is_a_nul_byte_near_the_start_not_a_suffix():
    assert public_gate.is_binary(b"PK\x03\x04\x00\x00rest")
    assert not public_gate.is_binary(b"plain text, no nul")
    # a NUL past the probe window does not make a huge text file binary
    assert not public_gate.is_binary(b"a" * public_gate.NUL_PROBE + b"\x00")


def test_ALX1544_P197_non_ascii_is_a_finding_and_the_echo_stays_ascii(vocab_file):
    assert public_gate.non_ascii("f.c", b"pure ascii\n") == []
    findings = public_gate.non_ascii("f.c", "first\n25 \N{DEGREE SIGN}C\n".encode())
    assert len(findings) == 1
    assert findings[0].startswith("NON-ASCII f.c:2:")
    assert findings[0].isascii()

    # a line that is BOTH non-ASCII and forbidden is quoted back without the replacement character
    vocab = public_gate.load(vocab_file)
    decoded = b"Widget 25 \xb0C".decode("utf-8", errors="replace")
    hit = vocab.hits("f.c", decoded)[0]
    assert hit.isascii(), hit
    assert "Widget" in hit


def test_ALX1544_P198_the_tree_scan_skips_what_it_should_and_reports_its_scope(
    tmp_path, vocab_file
):
    root = tmp_path / "tree"
    (root / "Vendor").mkdir(parents=True)
    (root / "__pycache__").mkdir()
    (root / "pkg.egg-info").mkdir()
    (root / "keep.c").write_text("Widget here\n", encoding="ascii")
    (root / "Vendor" / "theirs.c").write_text("Widget in vendor code\n", encoding="ascii")
    (root / "__pycache__" / "x.py").write_text("Widget cached\n", encoding="ascii")
    (root / "pkg.egg-info" / "PKG-INFO").write_text("Widget packaged\n", encoding="ascii")
    (root / "uv.lock").write_text("Widget locked\n", encoding="ascii")
    (root / "sheet.xlsx").write_bytes(b"PK\x03\x04\x00\x00Widget")

    findings, scope = public_gate.scan_tree(root, public_gate.load(vocab_file), ["Vendor"])
    assert findings == ["FORBIDDEN 'Widget'         keep.c:1: Widget here"]
    assert scope == "1 files, excluded: Vendor, 1 binary skipped (sheet.xlsx)"

    # with nothing excluded the vendor copy IS reported - the exclusion is the caller's choice
    findings, scope = public_gate.scan_tree(root, public_gate.load(vocab_file), [])
    assert len(findings) == 2
    assert "excluded" not in scope

    # more binaries than are named get an ellipsis, and a clean tree says so plainly
    for n in range(4):
        (root / f"b{n}.bin").write_bytes(b"\x00")
    _, scope = public_gate.scan_tree(root, public_gate.load(vocab_file), [])
    assert "5 binary skipped (b0.bin, b1.bin, b2.bin...)" in scope
    empty = tmp_path / "empty"
    empty.mkdir()
    assert public_gate.scan_tree(empty, Vocabulary(), []) == ([], "0 files")


def test_ALX1544_P199_commit_messages_are_scanned_and_a_missing_base_is_reported(repo, vocab_file):
    vocab = public_gate.load(vocab_file)
    findings, scope = public_gate.scan_messages(repo, vocab, "base", "HEAD")
    assert scope == "1 commit messages (base..HEAD)"
    assert len(findings) == 1
    assert "W-1234" in findings[0]

    findings, scope = public_gate.scan_messages(repo, vocab, "no-such-base", "HEAD")
    assert findings == []
    assert scope == "base no-such-base not found, commit messages not scanned"
    assert not public_gate.has_revision(repo, "no-such-base")
    assert public_gate.has_revision(repo, "base")

    # a non-ASCII commit message is a finding of its own
    _git(repo, "commit", "-q", "--allow-empty", "-m", "temperature 25 \N{DEGREE SIGN}C")
    findings, _ = public_gate.scan_messages(repo, vocab, "base", "HEAD")
    assert any(f.startswith("NON-ASCII commit ") for f in findings)


def test_ALX1544_P200_history_walks_every_commit_over_diffs_or_added_lines(repo, vocab_file):
    vocab = public_gate.load(vocab_file)
    findings, scope = public_gate.scan_history(repo, vocab, "base", "HEAD", diff_only=False)
    assert scope == "1 commits, full diffs (base..HEAD)"
    assert any("MSG" in f for f in findings)
    assert any("TREE" in f for f in findings)

    findings, scope = public_gate.scan_history(repo, vocab, "base", "HEAD", diff_only=True)
    assert scope == "1 commits, added lines (base..HEAD)"
    assert any("ADDED" in f and "Widget" in f for f in findings)
    assert public_gate.added_lines(repo, "HEAD") == "void WidgetBringUp(void) {}"

    findings, scope = public_gate.scan_history(repo, vocab, "no-such-base", "HEAD", diff_only=False)
    assert findings == []
    assert scope == "base no-such-base not found, history not scanned"
    assert public_gate.commits(repo, "base", "HEAD") != []


def test_ALX1544_P201_main_reports_pass_and_fail_and_honours_its_switches(repo, vocab_file, capsys):
    words = ["--words", str(vocab_file)]

    assert public_gate.main([str(repo), *words, "--base", "base"]) == 1
    printed = capsys.readouterr().out
    assert printed.splitlines()[0].startswith("PUBLIC GATE: FAIL (")
    assert "leak.c" in printed
    assert "commit " in printed

    # --commits-only leaves the tree alone: the message finding survives, the file one does not
    assert public_gate.main([str(repo), *words, "--base", "base", "--commits-only"]) == 1
    printed = capsys.readouterr().out
    assert "leak.c" not in printed
    assert "W-1234" in printed

    # a vocabulary that forbids nothing passes, and --out writes what was printed
    empty_vocab = repo / "none.json"
    empty_vocab.write_text("{}", encoding="ascii")
    out = repo / "reports" / "public_gate.txt"
    rc = public_gate.main(
        [str(repo), "--words", str(empty_vocab), "--base", "base", "--out", str(out)]
    )
    assert rc == 0
    printed = capsys.readouterr().out
    assert printed.splitlines()[0].startswith("PUBLIC GATE: PASS (")
    assert out.read_text(encoding="ascii") == printed

    assert public_gate.main([str(repo), *words, "--base", "base", "--history"]) == 1
    assert "TREE" in capsys.readouterr().out
    assert public_gate.main([str(repo), *words, "--base", "base", "--history", "--diff-only"]) == 1
    assert "ADDED" in capsys.readouterr().out

    # the vocabulary's own excludes are used even when --exclude adds none
    assert public_gate.main([str(repo), *words, "--base", "base", "--exclude", "extra"]) == 1


def test_ALX1544_P202_a_missing_vocabulary_is_a_reported_verdict_not_a_traceback(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv(public_gate.WORDS_ENV, raising=False)

    # nothing to read at all
    assert public_gate.main([str(tmp_path)]) == 1
    printed = capsys.readouterr().out
    assert printed.startswith("PUBLIC GATE: FAIL (no vocabulary:")
    assert "ALX_GATE_WORDS" in printed

    # a path that does not resolve, and the verdict still reaches --out
    out = tmp_path / "reports" / "gate.txt"
    assert (
        public_gate.main([str(tmp_path), "--words", str(tmp_path / "gone.json"), "--out", str(out)])
        == 1
    )
    printed = capsys.readouterr().out
    assert printed.startswith("PUBLIC GATE: FAIL (vocabulary file not found:")
    assert out.read_text(encoding="ascii") == printed
