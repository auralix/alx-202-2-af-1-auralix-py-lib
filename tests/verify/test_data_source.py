# SPDX-License-Identifier: MIT
"""alx.verify.data_source: every test data file identifies itself completely.

Proofs (ALX-1564):
  P345 a complete file passes the check; write() writes it as LF ASCII JSON and read() returns it;
       stamp() gives the UTC time in the record's form
  P346 each missing or empty source field, a field outside the record, wrong top-level keys and a
       file that is not JSON are findings, one line each; read() and write() refuse such a file
  P347 check() walks data/ at the root, nested folders included, and a root without data/ passes;
       main() exits 0 on PASS with the file count, 1 on FAIL, and --out writes the same report
"""

import json
import re

import pytest

from alx.verify import data_source

SOURCE = {
    "kind": "cli_items",
    "schema": 1,
    "captured": "2026-09-30T08:12:44Z",
    "tool": "harness.regen_goldens",
    "arguments": "--port COM12 --baud 115200",
    "device": {
        "name": "Fw",
        "ver": "0.1.0.2609300833.1a2b3c4",
        "bin": "2609300833_Fw_V0-1-0_1a2b3c4.bin",
    },
    "build": {"control_mode": "api"},
    "heads": {"fw": "1a2b3c4-dirty", "clib": "ccd51f0", "pylib": "518b192"},
}
DATA = {"get-param": ["A_Config_1", "B_Config_2"], "get-var": ["C_Var"]}


def test_ALX1564_P345_a_complete_file_round_trips_through_write_and_read(tmp_path):
    path = tmp_path / "data" / "cli_items.json"
    data_source.write(path, SOURCE, DATA)
    raw = path.read_bytes()
    assert b"\r" not in raw, "LF line ends"
    assert raw.endswith(b"}\n"), "one final newline"
    assert raw.decode("ascii").startswith('{\n  "source": {\n    "kind": "cli_items"')
    assert data_source.read(path) == {"source": SOURCE, "data": DATA}
    assert data_source.check_file(path) == []
    assert data_source.check(tmp_path) == []
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", data_source.stamp())


def test_ALX1564_P346_an_incomplete_record_is_a_finding_per_fault_and_is_refused(tmp_path):
    def findings_for(source, data=DATA):
        return data_source.check_content({"source": source, "data": data}, "x.json")

    for key in data_source.REQUIRED:
        without = {k: v for k, v in SOURCE.items() if k != key}
        assert findings_for(without) == [f"x.json: source.{key} missing"]
    empties = {
        "kind": "",
        "captured": "  ",
        "device": {},
        "build": None,
        "heads": {"fw": ""},
        "arguments": [],
        "tool": ["a", " "],
    }
    for key, value in empties.items():
        assert findings_for({**SOURCE, key: value}) == [f"x.json: source.{key} is empty"], key
    assert findings_for({**SOURCE, "port": "COM12"}) == [
        "x.json: source.port is not a field of the record"
    ], "the record has one set of fields, defined here"
    assert findings_for("a string") == ["x.json: source is a record of fields, not str"]
    two_keys = "x.json: a data file holds exactly two keys, source and data"
    assert data_source.check_content({"source": SOURCE, "items": DATA}, "x.json") == [two_keys]
    assert data_source.check_content({"source": SOURCE, "data": DATA, "note": 1}, "x.json") == [
        two_keys
    ]
    assert data_source.check_content([SOURCE, DATA], "x.json") == [two_keys]

    broken = tmp_path / "broken.json"
    broken.write_bytes(b"{")
    assert data_source.check_file(broken) == [
        "broken.json: not JSON, so it carries no source record"
    ]
    with pytest.raises(ValueError, match="not JSON"):
        data_source.read(broken)
    headless = tmp_path / "headless.json"
    headless.write_text(json.dumps(DATA), encoding="ascii")
    with pytest.raises(ValueError, match="exactly two keys"):
        data_source.read(headless)
    target = tmp_path / "data" / "refused.json"
    with pytest.raises(ValueError, match=r"source\.tool missing"):
        data_source.write(target, {k: v for k, v in SOURCE.items() if k != "tool"}, DATA)
    assert not target.exists(), "nothing is written for an incomplete record"


def test_ALX1564_P347_check_walks_the_root_data_folder_and_main_reports(tmp_path, capsys):
    assert data_source.check(tmp_path) == [], "a root without data/ has nothing to identify"
    assert data_source.main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == "DATA SOURCE GATE: PASS (0 files)\n"

    data_source.write(tmp_path / "data" / "a.json", SOURCE, DATA)
    data_source.write(tmp_path / "data" / "nested" / "b.json", {**SOURCE, "kind": "b"}, [1, 2])
    assert data_source.check(tmp_path) == []
    assert data_source.main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == "DATA SOURCE GATE: PASS (2 files)\n"

    (tmp_path / "data" / "nested" / "c.csv").write_text("1,2\n", encoding="ascii")
    out = tmp_path / "report" / "data_source.txt"
    assert data_source.main([str(tmp_path), "--out", str(out)]) == 1
    report = capsys.readouterr().out
    assert report == (
        "DATA SOURCE GATE: FAIL (1 finding(s))\n"
        "data/nested/c.csv: not JSON, so it carries no source record\n"
    )
    assert out.read_text(encoding="ascii") == report
