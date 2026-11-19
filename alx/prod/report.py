"""Append production results and preserve a log for every attempt."""

from __future__ import annotations

import csv
import io
import math
import os
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import TracebackType
    from typing import TextIO

COMMON_COLUMNS = (
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
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\uff1d", "\uff0b", "\uff0d", "\uff20")


class TestFailureError(Exception):
    """A production check failed."""

    __test__ = False


class OperatorAbortError(Exception):
    """The operator stopped the current attempt."""


TestFailure = TestFailureError
OperatorAbort = OperatorAbortError


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _formula(value: str) -> bool:
    return value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(_FORMULA_PREFIXES)


def _csv_value(value: object) -> object:
    if isinstance(value, str) and _formula(value):
        return "'" + value
    return value


def _finite(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
    )


def _lock(stream: TextIO) -> None:
    stream.seek(0)
    if sys.platform == "win32":
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _open_log(path: Path) -> TextIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o666)
    try:
        return os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
    except BaseException:
        os.close(descriptor)
        raise


class Report:
    """Keep one locked CSV table and an exclusive UTC-minute attempt log."""

    def __init__(
        self,
        output_dir: str | Path,
        serial_number: str,
        columns: Sequence[str],
        metadata: Mapping[str, object],
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], object] = print,
        *,
        clock_fn: Callable[[], datetime] = _utc_now,
        sleep_fn: Callable[[float], object] = time.sleep,
    ) -> None:
        """Validate the schema and identity before opening report files."""
        if (
            not serial_number
            or serial_number != serial_number.strip()
            or serial_number.endswith(".")
            or any(ord(char) < 32 or ord(char) == 127 for char in serial_number)
            or any(char in '<>:"/\\|?*' for char in serial_number)
            or _formula(serial_number)
        ):
            raise ValueError("Serial number is not safe for a filename and CSV cell")
        self.columns = tuple(columns)
        if (
            len(set(self.columns)) != len(self.columns)
            or not set(COMMON_COLUMNS).issubset(self.columns)
            or any(not column or _formula(column) for column in self.columns)
        ):
            raise ValueError("Columns must be unique and include all common columns")
        for field, value in metadata.items():
            if field not in self.columns:
                raise ValueError(f"Unknown metadata field: {field}")
            if field in {"utc_timestamp", "result", "note"} or (
                field == "serial_number" and value != serial_number
            ):
                raise ValueError(f"Conflicting metadata field: {field}")
        self.values: dict[str, object] = dict.fromkeys(self.columns, "")
        self.values.update(metadata)
        self.values["serial_number"] = serial_number
        self.output_dir = Path(output_dir)
        self.csv_path = self.output_dir / "test_report.csv"
        self.log_path: Path | None = None
        self._serial_number = serial_number
        self._timestamp = ""
        self._input = input_fn
        self._output = output_fn
        self._clock = clock_fn
        self._sleep = sleep_fn
        self._stack = ExitStack()
        self._csv: TextIO | None = None
        self._log: TextIO | None = None
        self._entered = False
        self._finished: tuple[str, str] | None = None
        self._failure_note = ""

    def __enter__(self) -> Report:
        """Lock and validate the results table before starting the attempt."""
        if self._entered:
            raise RuntimeError("A report cannot be entered more than once")
        self._entered = True
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as opening:
            stream = opening.enter_context(self.csv_path.open("a+", encoding="utf-8", newline=""))
            _lock(stream)
            self._csv = stream
            timestamps, _ = self._read_csv()
            while True:
                now = self._clock()
                if now.utcoffset() is None:
                    raise ValueError("The report clock must include a timezone")
                now = now.astimezone(UTC)
                timestamp = now.strftime("%y%m%d%H%M")
                log_path = self.output_dir / f"{timestamp}_{self._serial_number}.log"
                if timestamp not in timestamps:
                    try:
                        log_stream = _open_log(log_path)
                    except FileExistsError:
                        pass
                    else:
                        self._log = opening.enter_context(log_stream)
                        self.log_path = log_path
                        break
                self._output("An attempt already uses this UTC minute; waiting for the next minute")
                self._sleep(60 - now.second - now.microsecond / 1_000_000)
            self._timestamp = timestamp
            self.values["utc_timestamp"] = timestamp
            self.log(f"Start {timestamp} UTC; serial_number={self._serial_number}")
            self._stack = opening.pop_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record the final outcome, close both files, and propagate exceptions."""
        try:
            if self._finished is None:
                if isinstance(exc_value, TestFailure):
                    result = "FAIL"
                elif isinstance(exc_value, (OperatorAbort, KeyboardInterrupt)):
                    result = "ABORTED"
                elif exc_value is not None:
                    result = "ERROR"
                else:
                    result = "PASS"
                note = str(exc_value) or type(exc_value).__name__ if exc_value is not None else ""
                self.finish(result, note)
        finally:
            self._stack.close()

    def _active(self) -> None:
        if self._log is None or self._log.closed or self._finished is not None:
            raise RuntimeError("The report is not active")

    def _field(self, field: str) -> None:
        self._active()
        if field not in self.columns or field in COMMON_COLUMNS:
            raise ValueError(f"Unknown check or measurement field: {field}")

    def _read_csv(self) -> tuple[set[str], bool]:
        if self._csv is None:
            raise RuntimeError("The results table is not open")
        self._csv.seek(0)
        content = self._csv.read()
        if not content:
            csv.writer(self._csv).writerow(self.columns)
            self._csv.flush()
            os.fsync(self._csv.fileno())
            return set(), True
        reader = csv.reader(io.StringIO(content, newline=""), strict=True)
        if next(reader, None) != list(self.columns):
            raise ValueError("Existing CSV header does not match the report columns")
        serial_index = self.columns.index("serial_number")
        timestamp_index = self.columns.index("utc_timestamp")
        timestamps = set()
        for row in reader:
            if len(row) != len(self.columns):
                raise ValueError("Existing CSV contains an incomplete result row")
            if row[serial_index] == self._serial_number:
                timestamps.add(row[timestamp_index])
        return timestamps, content.endswith(("\r", "\n"))

    def log(self, message: str) -> None:
        """Write and flush one message before displaying it to the operator."""
        self._active()
        self._write_log(message)
        self._output(message)

    def _write_log(self, message: str) -> None:
        if self._log is not None:
            self._log.write(message + "\n")
            self._log.flush()

    def ask(self, prompt: str) -> str:
        """Log a prompt and answer, treating quit or interrupted input as an abort."""
        self._active()
        self._write_log(prompt)
        try:
            answer = self._input(prompt)
        except (EOFError, KeyboardInterrupt) as error:
            raise OperatorAbort("Operator input interrupted") from error
        self.log(f"> {answer}")
        if answer.strip().lower() in {"q", "abort"}:
            raise OperatorAbort("Operator requested abort")
        return answer

    def check(self, field: str, condition: bool, detail: str = "") -> None:
        """Record a check and stop on failure."""
        self._field(field)
        self.values[field] = "PASS" if condition else "FAIL"
        message = f"{field}: {self.values[field]}" + (f"; {detail}" if detail else "")
        self.log(message)
        if not condition:
            self._failure_note = message
            raise TestFailure(message)

    def measurement(
        self, field: str, value: float, low: float | None = None, high: float | None = None
    ) -> None:
        """Record a finite numeric measurement and enforce inclusive limits."""
        self._field(field)
        for limit in (low, high):
            if limit is not None and not _finite(limit):
                raise ValueError("Measurement limits must be finite numbers")
        if low is not None and high is not None and low > high:
            raise ValueError("The lower measurement limit exceeds the upper limit")
        valid = _finite(value)
        self.values[field] = value if valid else ""
        passed = valid and (low is None or value >= low) and (high is None or value <= high)
        message = f"{field}: {value}; low={low}; high={high}; {'PASS' if passed else 'FAIL'}"
        self.log(message)
        if not passed:
            self._failure_note = message
            raise TestFailure(message)

    def finish(self, result: str, note: str = "") -> None:
        """Append and sync one result row, without replacing prior attempts."""
        if result not in {"PASS", "FAIL", "ABORTED", "ERROR"}:
            raise ValueError(f"Invalid result: {result}")
        if result == "PASS" and self._failure_note:
            result = "FAIL"
            note = note or self._failure_note
        if self._finished is not None:
            if self._finished != (result, note):
                raise RuntimeError("The report already has a different final result")
            return
        self._active()
        if self._csv is None:
            raise RuntimeError("The results table is not open")
        if (
            self.values["serial_number"] != self._serial_number
            or self.values["utc_timestamp"] != self._timestamp
        ):
            raise ValueError("The attempt identity cannot change after the report starts")
        if set(self.values) != set(self.columns):
            raise ValueError("Report values do not match the report columns")
        _, has_newline = self._read_csv()
        self.values["result"] = result
        self.values["note"] = note
        message = f"Result: {result}" + (f"; {note}" if note else "")
        self._write_log(message)
        self._csv.seek(0, os.SEEK_END)
        if not has_newline:
            self._csv.write("\n")
        csv.writer(self._csv, quoting=csv.QUOTE_ALL).writerow(
            [_csv_value(self.values[field]) for field in self.columns]
        )
        self._csv.flush()
        self._finished = (result, note)
        os.fsync(self._csv.fileno())
        self._output(message)
