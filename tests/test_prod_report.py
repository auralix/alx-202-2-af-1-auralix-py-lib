import csv
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alx.prod import report

NOW = datetime(2026, 11, 19, 9, 20, 35, tzinfo=UTC)
COLUMNS = (*report.COMMON_COLUMNS, "continuity", "voltage_v", "unperformed")


def _report(
    output_dir,
    serial_number="00123-AB_9",
    metadata=None,
    input_fn=lambda _: "ready",
    output_fn=lambda _: None,
    clock_fn=lambda: NOW,
    sleep_fn=lambda _: None,
):
    return report.Report(
        output_dir,
        serial_number,
        COLUMNS,
        metadata or {},
        input_fn=input_fn,
        output_fn=output_fn,
        clock_fn=clock_fn,
        sleep_fn=sleep_fn,
    )


def _rows(output_dir):
    with (output_dir / "test_report.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_common_columns_have_the_required_stable_order():
    assert report.COMMON_COLUMNS == (
        "utc_timestamp",
        "serial_number",
        "pcb_name",
        "pcb_version",
        "pcb_ident",
        "bom_name",
        "bom_version",
        "bom_ident",
        "mcu_uid",
        "fw_version",
        "prod_test_version",
        "result",
        "note",
    )


def test_report_preserves_metadata_blank_fields_and_success(tmp_path):
    metadata = {"pcb_name": "Example", "fw_version": "1.2.3", "serial_number": "00123-AB_9"}
    with _report(tmp_path, metadata=metadata) as result:
        assert result.values["unperformed"] == ""
        assert result.values["bom_ident"] == ""
        result.values["mcu_uid"] = "0123456789ABCDEF"
        result.check("continuity", True, "closed")
        result.measurement("voltage_v", 3.3, low=3.0, high=3.6)
    (row,) = _rows(tmp_path)
    assert row["utc_timestamp"] == "2611190920"
    assert row["serial_number"] == "00123-AB_9"
    assert row["pcb_name"] == "Example"
    assert row["fw_version"] == "1.2.3"
    assert row["mcu_uid"] == "0123456789ABCDEF"
    assert row["continuity"] == "PASS"
    assert float(row["voltage_v"]) == 3.3
    assert row["result"] == "PASS"
    assert row["unperformed"] == ""
    assert row["bom_ident"] == ""
    assert {path.name for path in tmp_path.iterdir()} == {
        ".test_report.lock",
        "test_report.csv",
        "2611190920_00123-AB_9.log",
    }


def test_log_and_prompt_are_flushed_before_the_run_finishes(tmp_path):
    displayed: list[str] = []
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return 'ready, "yes"'

    with _report(tmp_path, input_fn=answer, output_fn=displayed.append) as result:
        result.log("Connecting")
        assert "Connecting" in (tmp_path / "2611190920_00123-AB_9.log").read_text("utf-8")
        assert result.ask("Continue?") == 'ready, "yes"'
        log = (tmp_path / "2611190920_00123-AB_9.log").read_text("utf-8")
        assert "Continue?" in log
        assert 'ready, "yes"' in log
    assert any("Connecting" in line for line in displayed)
    assert prompts == ["Continue?"]


def test_report_and_external_append_stream_preserve_interleaved_log_messages(tmp_path):
    log_path = tmp_path / "2611190920_00123-AB_9.log"
    expected = ["Start 2611190920 UTC; serial_number=00123-AB_9\n"]
    messages = [
        ("Report first", "External first"),
        ("Report caf\u00e9\ncontinued", "External \u6e2c\u8a66\ncontinued"),
        ("Report third", "External longer message that must remain intact"),
    ]
    with (
        _report(tmp_path) as result,
        log_path.open("a", encoding="utf-8", newline="\n") as external,
    ):
        for report_message, external_message in messages:
            result.log(report_message)
            external.write(external_message + "\n")
            external.flush()
            expected.extend([report_message + "\n", external_message + "\n"])
        result.log("Report final")
        expected.append("Report final\n")
        assert log_path.read_text("utf-8") == "".join(expected)
    expected.append("Result: PASS\n")
    assert log_path.read_text("utf-8") == "".join(expected)


def test_log_stream_open_failure_closes_reserved_descriptor_and_releases_station_lock(
    tmp_path, monkeypatch
):
    descriptors: list[int] = []

    def fail_open(descriptor, *args, **kwargs):
        descriptors.append(descriptor)
        raise OSError("Log stream failed to open")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", fail_open)
        with pytest.raises(OSError, match="Log stream failed to open"), _report(tmp_path):
            pytest.fail("Report entered without an open log")
    (descriptor,) = descriptors
    with pytest.raises(OSError, match=r"[Bb]ad file descriptor|[Hh]andle is invalid"):
        os.fstat(descriptor)
    with _report(tmp_path, serial_number="A-02"):
        pass
    (row,) = _rows(tmp_path)
    assert row["serial_number"] == "A-02"
    assert row["result"] == "PASS"


def test_failed_check_records_failure_and_propagates(tmp_path):
    with pytest.raises(report.TestFailure, match="open circuit"), _report(tmp_path) as result:
        result.check("continuity", False, "open circuit")
    (row,) = _rows(tmp_path)
    assert row["continuity"] == "FAIL"
    assert row["result"] == "FAIL"
    assert "open circuit" in row["note"]
    assert row["unperformed"] == ""


@pytest.mark.parametrize("measurement", [False, True])
def test_caught_failure_cannot_be_reported_as_pass(tmp_path, measurement):
    with _report(tmp_path) as result:

        def fail():
            if measurement:
                result.measurement("voltage_v", 2.0, low=3.0)
            else:
                result.check("continuity", False, "open circuit")

        with pytest.raises(report.TestFailure):
            fail()
        result.finish("PASS")
    (row,) = _rows(tmp_path)
    assert row["result"] == "FAIL"
    assert row["note"]


@pytest.mark.parametrize("answer", ["q", " Q ", "abort", " ABORT "])
def test_abort_answer_is_logged_and_propagates(tmp_path, answer):
    with (
        pytest.raises(report.OperatorAbort),
        _report(tmp_path, input_fn=lambda _: answer) as result,
    ):
        result.ask("Continue?")
    assert _rows(tmp_path)[0]["result"] == "ABORTED"
    log = (tmp_path / "2611190920_00123-AB_9.log").read_text("utf-8")
    assert "Continue?" in log
    assert answer in log


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_input_interruption_becomes_operator_abort(tmp_path, interruption):
    def interrupted(_prompt):
        raise interruption

    with (
        pytest.raises(report.OperatorAbort),
        _report(tmp_path, input_fn=interrupted) as result,
    ):
        result.ask("Continue?")
    assert _rows(tmp_path)[0]["result"] == "ABORTED"


@pytest.mark.parametrize(
    ("exception", "expected"),
    [(RuntimeError("disconnected"), "ERROR"), (KeyboardInterrupt(), "ABORTED")],
)
def test_context_records_and_propagates_unexpected_exceptions(tmp_path, exception, expected):
    with pytest.raises(type(exception)), _report(tmp_path):
        raise exception
    assert _rows(tmp_path)[0]["result"] == expected


@pytest.mark.parametrize("value", [3.0, 3.3, 3.6])
def test_measurement_accepts_inclusive_limits_and_records_numeric_value(tmp_path, value):
    with _report(tmp_path) as result:
        result.measurement("voltage_v", value, low=3.0, high=3.6)
        assert result.values["voltage_v"] == value
    log = (tmp_path / "2611190920_00123-AB_9.log").read_text("utf-8")
    assert "voltage_v" in log
    assert "3.0" in log
    assert "3.6" in log


@pytest.mark.parametrize("value", [2.9, 3.7])
def test_out_of_range_measurement_records_value_and_fails(tmp_path, value):
    with pytest.raises(report.TestFailure), _report(tmp_path) as result:
        result.measurement("voltage_v", value, low=3.0, high=3.6)
    (row,) = _rows(tmp_path)
    assert float(row["voltage_v"]) == value
    assert row["result"] == "FAIL"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "invalid", True])
def test_measurement_rejects_nonfinite_and_nonnumeric_values(tmp_path, value):
    with pytest.raises(report.TestFailure), _report(tmp_path) as result:
        result.measurement("voltage_v", value)
    assert _rows(tmp_path)[0]["result"] == "FAIL"


@pytest.mark.parametrize(
    ("low", "high"),
    [(4.0, 3.0), (float("nan"), 3.6), (3.0, float("inf"))],
)
def test_measurement_rejects_invalid_limits(tmp_path, low, high):
    with pytest.raises(ValueError, match=r"[Ll]imit|[Ll]ow|[Hh]igh"), _report(tmp_path) as result:
        result.measurement("voltage_v", 3.3, low=low, high=high)
    assert _rows(tmp_path)[0]["result"] == "ERROR"


def test_csv_round_trips_commas_quotes_and_multiline_notes(tmp_path):
    note = 'First line, "quoted"\nSecond line'
    with _report(tmp_path, metadata={"pcb_name": 'Example, "A"'}) as result:
        result.finish("PASS", note)
    (row,) = _rows(tmp_path)
    assert row["pcb_name"] == 'Example, "A"'
    assert row["note"] == note


@pytest.mark.parametrize("note", ["=1+1", "+1+1", "-1+1", "@SUM(1)", "  =1+1", "\t=1+1"])
def test_csv_escapes_formula_like_text_without_changing_in_memory_value(tmp_path, note):
    with _report(tmp_path, metadata={"pcb_name": note}) as result:
        result.finish("PASS", note)
        assert result.values["pcb_name"] == note
        assert result.values["note"] == note
    (row,) = _rows(tmp_path)
    assert row["pcb_name"] == "'" + note
    assert row["note"] == "'" + note


def test_header_mismatch_stops_before_context_body_and_preserves_file(tmp_path):
    results_path = tmp_path / "test_report.csv"
    previous = b"serial_number,result\r\nold,PASS\r\n"
    results_path.write_bytes(previous)
    entered = []
    with pytest.raises(ValueError, match="header"), _report(tmp_path):
        entered.append(True)
    assert entered == []
    assert results_path.read_bytes() == previous
    assert list(tmp_path.glob("*.log")) == []


def test_incomplete_existing_row_stops_before_context_body(tmp_path):
    results_path = tmp_path / "test_report.csv"
    with results_path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerow(COLUMNS)
        csv.writer(stream).writerow(["2611190900", "A-01"])
    previous = results_path.read_bytes()
    with pytest.raises(ValueError, match="incomplete"), _report(tmp_path):
        pytest.fail("Incomplete results table was accepted")
    assert results_path.read_bytes() == previous
    assert list(tmp_path.glob("*.log")) == []


def test_repeated_serial_waits_for_a_real_new_minute_without_overwriting(tmp_path):
    clock = [NOW]
    sleeps = []

    def wait(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    with _report(tmp_path, clock_fn=lambda: clock[0]) as result:
        result.log("First attempt")
    first_log = (tmp_path / "2611190920_00123-AB_9.log").read_bytes()
    with _report(tmp_path, clock_fn=lambda: clock[0], sleep_fn=wait) as result:
        result.log("Second attempt")
    assert sleeps == [25.0]
    assert (tmp_path / "2611190920_00123-AB_9.log").read_bytes() == first_log
    assert (tmp_path / "2611190921_00123-AB_9.log").is_file()
    assert [row["utc_timestamp"] for row in _rows(tmp_path)] == ["2611190920", "2611190921"]
    assert [row["serial_number"] for row in _rows(tmp_path)] == ["00123-AB_9", "00123-AB_9"]
    assert len(list(tmp_path.iterdir())) == 4


def test_different_serials_append_rows_in_the_same_minute(tmp_path):
    with _report(tmp_path, serial_number="A-01"):
        pass
    first_csv = (tmp_path / "test_report.csv").read_bytes()
    with _report(tmp_path, serial_number="A-02"):
        pass
    assert (tmp_path / "test_report.csv").read_bytes().startswith(first_csv)
    assert [row["serial_number"] for row in _rows(tmp_path)] == ["A-01", "A-02"]


@pytest.mark.parametrize("existing_result", [False, True])
def test_either_existing_csv_attempt_or_log_prevents_timestamp_reuse(tmp_path, existing_result):
    clock = [NOW]
    previous_log = tmp_path / "2611190920_00123-AB_9.log"
    if existing_result:
        with _report(tmp_path):
            pass
        previous_log.unlink()
    else:
        previous_log.write_text("Interrupted attempt\n", encoding="utf-8")

    def wait(seconds):
        clock[0] += timedelta(seconds=seconds)

    with _report(tmp_path, clock_fn=lambda: clock[0], sleep_fn=wait):
        pass
    assert _rows(tmp_path)[-1]["utc_timestamp"] == "2611190921"
    assert (tmp_path / "2611190921_00123-AB_9.log").is_file()
    if not existing_result:
        assert previous_log.read_text("utf-8") == "Interrupted attempt\n"


def test_append_preserves_a_complete_existing_row_without_a_final_newline(tmp_path):
    with _report(tmp_path, serial_number="A-01"):
        pass
    results_path = tmp_path / "test_report.csv"
    previous = results_path.read_bytes().rstrip(b"\r\n")
    results_path.write_bytes(previous)
    with _report(tmp_path, serial_number="A-02"):
        pass
    assert results_path.read_bytes().startswith(previous)
    assert [row["serial_number"] for row in _rows(tmp_path)] == ["A-01", "A-02"]


def test_identical_finish_and_context_exit_write_only_one_row(tmp_path):
    with _report(tmp_path) as result:
        result.finish("PASS", "complete")
        result.finish("PASS", "complete")
    assert len(_rows(tmp_path)) == 1
    assert _rows(tmp_path)[0]["note"] == "complete"


def test_finishing_flushes_the_row_to_disk(tmp_path, monkeypatch):
    synced = []
    original = os.fsync

    def fsync(fd):
        synced.append(fd)
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with _report(tmp_path) as result:
        before_finish = len(synced)
        result.finish("PASS")
        assert len(synced) > before_finish
    assert _rows(tmp_path)[0]["result"] == "PASS"


def test_conflicting_finish_cannot_replace_an_existing_result(tmp_path):
    with _report(tmp_path) as result:
        result.finish("FAIL", "failed")
        with pytest.raises(RuntimeError):
            result.finish("PASS", "changed")
    (row,) = _rows(tmp_path)
    assert row["result"] == "FAIL"
    assert row["note"] == "failed"


@pytest.mark.parametrize(
    "metadata",
    [
        {"serial_number": "different"},
        {"utc_timestamp": "2611190000"},
        {"result": "PASS"},
        {"note": "preset"},
        {"unknown": "value"},
    ],
)
def test_conflicting_or_unknown_metadata_is_rejected_before_output(tmp_path, metadata):
    with pytest.raises(ValueError, match=r"[Mm]etadata|[Ss]erial|[Rr]eserved|[Uu]nknown"):
        _report(tmp_path, metadata=metadata)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "serial_number",
    [
        "",
        "../item",
        "a/b",
        "a\\b",
        "a:b",
        "a*",
        "a?",
        'a"',
        "a<",
        "a>",
        "a|",
        "a\n",
        "a\x00",
        "a.",
        "a ",
        "=1+1",
        "@SUM(1)",
        " +1",
    ],
)
def test_unsafe_serials_are_rejected_without_creating_output(tmp_path, serial_number):
    with pytest.raises(ValueError, match=r"[Ss]erial"):
        _report(tmp_path, serial_number=serial_number)
    assert list(tmp_path.iterdir()) == []


def test_unknown_measurement_and_check_fields_are_rejected(tmp_path):
    with _report(tmp_path) as result:
        with pytest.raises(ValueError, match="unknown"):
            result.check("unknown", True)
        with pytest.raises(ValueError, match="unknown"):
            result.measurement("unknown", 1.0)


def test_a_second_runner_cannot_enter_the_same_output_directory(tmp_path):
    with (
        _report(tmp_path, serial_number="A-01"),
        pytest.raises((OSError, RuntimeError)),
        _report(tmp_path, serial_number="A-02"),
    ):
        pytest.fail("Concurrent report entered")
    with _report(tmp_path, serial_number="A-03"):
        pass
    assert [row["serial_number"] for row in _rows(tmp_path)] == ["A-01", "A-03"]


def _upgraded_report(output_dir, previous_columns=COLUMNS):
    return report.Report(
        output_dir,
        "A-03",
        (*COLUMNS, "new_measurement"),
        {"prod_test_version": "0.2.0+new-source-hash"},
        previous_columns=previous_columns,
        output_fn=lambda _: None,
        clock_fn=lambda: NOW,
    )


def test_explicit_schema_upgrade_preserves_records_versions_logs_and_original_bytes(tmp_path):
    for serial_number in ("A-01", "A-02"):
        with _report(
            tmp_path,
            serial_number=serial_number,
            metadata={"prod_test_version": "0.1.0+old-source-hash", "pcb_name": 'Example, "A"'},
        ) as result:
            result.measurement("voltage_v", 3.3, low=3.0, high=3.6)
            result.finish("PASS", 'Multiline \u00e9vidence\nwith "quotes", commas')
    csv_path = tmp_path / "test_report.csv"
    original = csv_path.read_bytes()
    old_rows = _rows(tmp_path)
    old_logs = {path: path.read_bytes() for path in tmp_path.glob("*.log")}
    with _upgraded_report(tmp_path) as result:
        assert (tmp_path / "test_report.csv.bak").read_bytes() == original
        result.measurement("new_measurement", 7.0, low=6.0, high=8.0)
    updated_rows = _rows(tmp_path)
    for old, updated in zip(old_rows, updated_rows[:2], strict=True):
        assert {field: updated[field] for field in COLUMNS} == old
        assert updated["new_measurement"] == ""
    assert updated_rows[2]["prod_test_version"] == "0.2.0+new-source-hash"
    assert updated_rows[2]["new_measurement"] == "7.0"
    assert updated_rows[2]["result"] == "PASS"
    assert {path: path.read_bytes() for path in old_logs} == old_logs
    assert not list(tmp_path.glob("*.tmp"))


def test_schema_upgrade_is_opt_in_and_unknown_old_header_never_gets_rewritten(tmp_path):
    with _report(tmp_path, serial_number="A-01"):
        pass
    original = (tmp_path / "test_report.csv").read_bytes()
    with (
        pytest.raises(ValueError, match="header"),
        _upgraded_report(tmp_path, previous_columns=None),
    ):
        pytest.fail("Unexpected schema was accepted")
    assert (tmp_path / "test_report.csv").read_bytes() == original
    assert not (tmp_path / "test_report.csv.bak").exists()
    assert len(list(tmp_path.glob("*.log"))) == 1


@pytest.mark.parametrize(
    "previous_columns",
    [
        (),
        (*COLUMNS, "voltage_v"),
        (*COLUMNS, "new_measurement"),
        (*COLUMNS, "removed_measurement"),
        COLUMNS[1:],
    ],
)
def test_schema_upgrade_rejects_ambiguous_deleted_or_renamed_columns_before_output(
    tmp_path, previous_columns
):
    with pytest.raises(ValueError, match="Previous columns"):
        _upgraded_report(tmp_path, previous_columns=previous_columns)
    assert list(tmp_path.iterdir()) == []


def test_upgrade_rejects_incomplete_old_rows_before_backup_or_log(tmp_path):
    path = tmp_path / "test_report.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerow(COLUMNS)
        csv.writer(stream).writerow(["2611190900", "A-01"])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="incomplete"), _upgraded_report(tmp_path):
        pytest.fail("Incomplete record was migrated")
    assert path.read_bytes() == original
    assert not (tmp_path / "test_report.csv.bak").exists()
    assert not list(tmp_path.glob("*.log"))


def test_upgrade_rejects_malformed_csv_without_touching_original(tmp_path):
    path = tmp_path / "test_report.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerow(COLUMNS)
        stream.write('"unterminated')
    original = path.read_bytes()
    with pytest.raises(csv.Error), _upgraded_report(tmp_path):
        pytest.fail("Malformed record was migrated")
    assert path.read_bytes() == original
    assert not (tmp_path / "test_report.csv.bak").exists()


@pytest.mark.parametrize("matches", [False, True])
def test_upgrade_never_overwrites_an_existing_backup(tmp_path, matches):
    with _report(tmp_path, serial_number="A-01"):
        pass
    path = tmp_path / "test_report.csv"
    original = path.read_bytes()
    backup = tmp_path / "test_report.csv.bak"
    previous_backup = original if matches else b"Older untouched archive\n"
    backup.write_bytes(previous_backup)
    if matches:
        with _upgraded_report(tmp_path):
            pass
        assert _rows(tmp_path)[1]["result"] == "PASS"
    else:
        with pytest.raises(ValueError, match="backup differs"), _upgraded_report(tmp_path):
            pytest.fail("Conflicting archive was ignored")
        assert path.read_bytes() == original
        assert len(list(tmp_path.glob("*.log"))) == 1
    assert backup.read_bytes() == previous_backup


def test_failed_atomic_replace_keeps_original_backup_and_releases_lock(tmp_path, monkeypatch):
    with _report(tmp_path, serial_number="A-01"):
        pass
    path = tmp_path / "test_report.csv"
    original = path.read_bytes()

    def fail_replace(self, target):
        assert target == path
        assert path.read_bytes() == original
        raise OSError("Replace failed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="Replace failed"), _upgraded_report(tmp_path):
            pytest.fail("Failed replacement entered attempt")
    assert path.read_bytes() == original
    assert (tmp_path / "test_report.csv.bak").read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
    assert len(list(tmp_path.glob("*.log"))) == 1
    with _upgraded_report(tmp_path):
        pass
    assert [row["serial_number"] for row in _rows(tmp_path)] == ["A-01", "A-03"]


def test_upgrade_cannot_run_while_an_attempt_holds_writer_lock(tmp_path):
    with _report(tmp_path, serial_number="A-01"):
        with pytest.raises((OSError, RuntimeError)), _upgraded_report(tmp_path):
            pytest.fail("Upgrade ran concurrently with production")
        assert not (tmp_path / "test_report.csv.bak").exists()
    with _upgraded_report(tmp_path):
        pass


def test_upgrade_detects_old_runner_holding_csv_lock(tmp_path):
    with _report(tmp_path, serial_number="A-01"):
        pass
    path = tmp_path / "test_report.csv"
    original = path.read_bytes()
    with path.open("a+", encoding="utf-8", newline="") as old_writer:
        report._lock(old_writer)
        with pytest.raises((OSError, RuntimeError)), _upgraded_report(tmp_path):
            pytest.fail("Old writer lock was ignored")
    assert path.read_bytes() == original
    assert not (tmp_path / "test_report.csv.bak").exists()


def test_current_schema_appends_without_remigrating_or_overwriting_backup(tmp_path):
    with _report(tmp_path, serial_number="A-01"):
        pass
    with _upgraded_report(tmp_path):
        pass
    backup = (tmp_path / "test_report.csv.bak").read_bytes()
    before = (tmp_path / "test_report.csv").read_bytes()
    with report.Report(
        tmp_path,
        "A-04",
        (*COLUMNS, "new_measurement"),
        {},
        previous_columns=COLUMNS,
        output_fn=lambda _: None,
        clock_fn=lambda: NOW,
    ):
        pass
    assert (tmp_path / "test_report.csv").read_bytes().startswith(before)
    assert (tmp_path / "test_report.csv.bak").read_bytes() == backup
