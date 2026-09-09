# SPDX-License-Identifier: MIT
"""alx.serial_logger - the long-term UART logger over a scripted port (no device, no real process).

The fake port replays a script: bytes chunks, a float (silence for that long), or "LOST" (the port
disappears). The factory hands out fake ports in order, or refuses to open ("NOPORT").

Proofs (ALX-1544):
  P80 every device line is logged with a wall-clock timestamp, [INFO] level and the start / stop marks
  P81 a line without terminator is flushed as "(partial)" after idle_flush_s
  P82 silence produces a heartbeat line every heartbeat_s
  P83 a lost port is logged as a gap and reopened; lines keep flowing, gaps are counted
  P84 a port that cannot be opened at start is retried until it can
  P85 stop() closes the port and writes the stop mark with the counters
  P86 undecodable bytes are replaced, never dropped
  P87 start_detached / status / stop_detached manage the PID file and the process
  P88 main(): run drives SerialLogger.run, status exits non-zero when nothing runs
  P89 the log file is named after the folder
  P90 a detached logger that dies at start raises and leaves no PID file
  P91 _pid_alive / _pid_kill drive the Windows process tools (tasklist filter, taskkill /F)
  P92 main(): start reports the PID and the log path
  P93 a port that never opens is logged with the retry period; stop() is idempotent
  P94 a partial line still pending at stop is flushed as "(partial)"
  P124 mutation-driven hardening: one heartbeat per heartbeat_s of silence, not one per loop turn
  P125 mutation-driven hardening: a PID file with garbage reads as no pid, never a crash
  P126 mutation-driven hardening: a stale PID file (process gone) is cleaned without a kill
  P127 mutation-driven hardening: CLI defaults (115200 baud, 600 s heartbeat, 90 days) and --baud is an int
"""

import re
import subprocess
import sys
import time

import pytest
import serial

import alx.serial_logger as sl
from alx.serial_logger import SerialLogger

STAMP = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \[(INFO|WARNING)\] "


class FakePort:
    def __init__(self, script):
        self.script = list(script)
        self.closed = False

    def read_until(self, terminator=b"\n"):
        if not self.script:
            time.sleep(0.01)
            return b""
        item = self.script.pop(0)
        if item == "LOST":
            raise serial.SerialException("device gone")
        if isinstance(item, (int, float)):
            time.sleep(item)
            return b""
        return item

    def close(self):
        self.closed = True


class Factory:
    """Hands out the scripted ports in order ("NOPORT" refuses to open); records what it opened."""

    def __init__(self, ports):
        self.queue = list(ports)
        self.opened: list[FakePort] = []

    def __call__(self, port, baud, timeout):
        assert timeout == 1.0
        item = self.queue.pop(0) if self.queue else FakePort([])
        if item == "NOPORT":
            raise serial.SerialException("could not open port")
        self.opened.append(item)
        return item


def make_factory(ports):
    return Factory(ports)


def run_logger(tmp_path, ports, run_s=0.3, **kw):
    kw.setdefault("heartbeat_s", 60.0)
    logger = SerialLogger(
        "FAKE1", 115200, tmp_path / "soak", serial_factory=make_factory(ports), **kw
    )
    logger.start()
    time.sleep(run_s)
    logger.stop()
    return logger, logger.log_path.read_text(encoding="utf-8")


def test_ALX1544_P80_lines_are_timestamped_and_bracketed_by_marks(tmp_path):
    logger, text = run_logger(tmp_path, [FakePort([b"boot\r\n", b"line two\r\n"])])
    lines = text.splitlines()
    assert all(re.match(STAMP, ln) for ln in lines), lines
    assert "-- start: port FAKE1, 115200 baud" in lines[0]
    assert "-- port FAKE1 open" in lines[1]
    assert lines[2].endswith("] boot")
    assert lines[3].endswith("] line two")
    assert lines[-1].endswith("-- stop: 2 lines, 0 port gaps")
    assert logger.lines == 2
    assert logger.gaps == 0


def test_ALX1544_P81_partial_line_is_flushed_after_idle(tmp_path):
    _logger, text = run_logger(tmp_path, [FakePort([b"no newline"])], idle_flush_s=0.05)
    assert "] (partial) no newline" in text


def test_ALX1544_P82_silence_produces_heartbeats(tmp_path):
    _logger, text = run_logger(tmp_path, [FakePort([])], run_s=0.3, heartbeat_s=0.05)
    beats = [ln for ln in text.splitlines() if "-- heartbeat: no data for" in ln]
    assert len(beats) >= 2, text


def test_ALX1544_P83_lost_port_is_logged_reopened_and_counted(tmp_path):
    first, second = FakePort([b"a\r\n", "LOST"]), FakePort([b"b\r\n", "LOST"])
    third = FakePort([b"c\r\n"])
    logger, text = run_logger(tmp_path, [first, second, third], reconnect_s=0.02)
    assert text.count("-- port FAKE1 lost: device gone") == 2
    assert text.count("-- port FAKE1 open") == 3
    assert "] a" in text
    assert "] b" in text
    assert "] c" in text
    assert first.closed
    assert second.closed
    assert logger.gaps == 2, "every loss counts"
    assert logger.lines == 3


def test_ALX1544_P84_unopenable_port_is_retried(tmp_path):
    port = FakePort([b"x\r\n"])
    _logger, text = run_logger(tmp_path, ["NOPORT", "NOPORT", port], reconnect_s=0.02)
    assert text.count("-- port FAKE1 not open") == 1, "the retry is announced once, not every 20 ms"
    assert "-- port FAKE1 open" in text
    assert "] x" in text


def test_ALX1544_P85_stop_closes_the_port_and_writes_the_counters(tmp_path):
    port = FakePort([b"one\r\n"])
    logger, text = run_logger(tmp_path, [port])
    assert port.closed
    assert text.splitlines()[-1].endswith("-- stop: 1 lines, 0 port gaps")
    assert logger._thread is None


def test_ALX1544_P86_undecodable_bytes_are_replaced(tmp_path):
    _logger, text = run_logger(tmp_path, [FakePort([b"caf\xe9\r\n"])])
    assert "] caf\ufffd" in text


def test_ALX1544_P87_detached_lifecycle_pid_file_status_stop(tmp_path, monkeypatch):
    log_dir = tmp_path / "soak"
    popen_calls = []
    killed: list[int] = []
    alive = {"value": True}

    class FakeProc:
        pid = 4242

    def fake_popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sl, "_pid_alive", lambda pid: alive["value"] and pid == 4242)
    monkeypatch.setattr(sl, "_pid_kill", killed.append)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    pid = sl.start_detached("FAKE1", 115200, log_dir, heartbeat_s=30.0, retention_days=7)
    assert pid == 4242
    assert (log_dir / "serial_logger.pid").read_text() == "4242"
    args, kwargs = popen_calls[0]
    assert args[1:4] == ["-m", "alx.serial_logger", "run"]
    assert args[4:] == [
        "--port",
        "FAKE1",
        "--baud",
        "115200",
        "--dir",
        str(log_dir),
        "--heartbeat-s",
        "30.0",
        "--retention-days",
        "7",
    ]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL

    (log_dir / "soak.log").write_text(
        "2026-01-01 00:00:00.000 [INFO] last line\n", encoding="utf-8"
    )
    info = sl.status(log_dir)
    assert info == {
        "pid": 4242,
        "alive": True,
        "log": str(log_dir / "soak.log"),
        "last": "2026-01-01 00:00:00.000 [INFO] last line",
    }

    sl.start_detached("FAKE1", 115200, log_dir)
    assert killed == [4242], "a second start stops the running logger first (one owner per port)"

    assert sl.stop_detached(log_dir) is True
    assert killed == [4242, 4242]
    assert not (log_dir / "serial_logger.pid").exists()
    assert sl.stop_detached(log_dir) is False


def test_ALX1544_P88_main_run_and_status(tmp_path, monkeypatch, capsys):
    ran: dict[str, object] = {}

    def fake_run(self):
        ran.update(
            port=self.port,
            baud=self.baud,
            dir=self.log_dir,
            hb=self.heartbeat_s,
            keep=self.retention_days,
        )

    monkeypatch.setattr(SerialLogger, "run", fake_run)
    assert (
        sl.main(
            [
                "run",
                "--port",
                "FAKE1",
                "--baud",
                "9600",
                "--dir",
                str(tmp_path / "s"),
                "--heartbeat-s",
                "5",
                "--retention-days",
                "3",
            ]
        )
        == 0
    )
    assert ran == {"port": "FAKE1", "baud": 9600, "dir": tmp_path / "s", "hb": 5.0, "keep": 3}
    monkeypatch.setattr(sl, "_pid_alive", lambda pid: False)
    assert sl.main(["status", "--dir", str(tmp_path / "s")]) == 1
    assert "pid None alive False" in capsys.readouterr().out
    assert sl.main(["stop", "--dir", str(tmp_path / "s")]) == 0
    assert "no logger was running" in capsys.readouterr().out


def test_ALX1544_P89_log_file_is_named_after_the_folder(tmp_path):
    logger = SerialLogger(
        "FAKE1", 115200, tmp_path / "week_37", serial_factory=make_factory([FakePort([])])
    )
    assert logger.log_path == tmp_path / "week_37" / "week_37.log"
    assert logger.name == "week_37"


def test_ALX1544_P90_detached_logger_dying_at_start_raises_and_cleans_up(tmp_path, monkeypatch):
    class FakeProc:
        pid = 99

    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: FakeProc())
    monkeypatch.setattr(sl, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(sl, "_pid_kill", lambda pid: None)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="died at start"):
        sl.start_detached("FAKE1", 115200, tmp_path / "soak")
    assert not (tmp_path / "soak" / "serial_logger.pid").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows process tools")
def test_ALX1544_P91_pid_alive_and_kill_use_the_windows_process_tools(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = "python.exe    4242 Console   1   20,000 K\n" if cmd[0] == "tasklist" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert sl._pid_alive(4242) is True
    assert sl._pid_alive(4243) is False
    assert calls[0][:3] == ["tasklist", "/FI", "PID eq 4242"]
    sl._pid_kill(4242)
    assert calls[-1] == ["taskkill", "/PID", "4242", "/F"]


def test_ALX1544_P92_main_start_reports_pid_and_log(tmp_path, monkeypatch, capsys):
    seen: dict[str, object] = {}

    def fake_start(port, baud, log_dir, heartbeat_s, retention_days):
        seen.update(port=port, baud=baud, dir=str(log_dir), hb=heartbeat_s, keep=retention_days)
        return 777

    monkeypatch.setattr(sl, "start_detached", fake_start)
    soak = tmp_path / "soak"
    argv = [
        "start",
        "--port",
        "FAKE1",
        "--dir",
        str(soak),
        "--heartbeat-s",
        "30",
        "--retention-days",
        "7",
    ]
    assert sl.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("serial logger started: pid 777, ")
    assert out.rstrip().endswith("soak.log")
    assert seen == {"port": "FAKE1", "baud": 115200, "dir": str(soak), "hb": 30.0, "keep": 7}


def test_ALX1544_P93_a_port_that_never_opens_is_logged_and_stop_is_idempotent(tmp_path):
    logger, text = run_logger(tmp_path, ["NOPORT"] * 50, run_s=0.15, reconnect_s=0.02)
    lines = text.splitlines()
    assert re.match(STAMP + r"-- start: port FAKE1", lines[0])
    assert any("port FAKE1 not open" in ln and "retrying every 0 s" in ln for ln in lines)
    assert lines[-1].endswith("-- stop: 0 lines, 1 port gaps")
    assert logger.gaps == 1, "failed opens count as one gap until the port appears"
    logger.stop()  # a second stop is a no-op


def test_ALX1544_P94_a_partial_line_pending_at_stop_is_flushed(tmp_path):
    _, text = run_logger(tmp_path, [FakePort([b"no newline yet"])], run_s=0.15, idle_flush_s=10.0)
    assert "(partial) no newline yet" in text
    assert text.splitlines()[-1].endswith("-- stop: 1 lines, 0 port gaps")


def test_ALX1544_P124_one_heartbeat_per_heartbeat_period(tmp_path):
    _, text = run_logger(tmp_path, [FakePort([])], run_s=0.35, heartbeat_s=0.1)
    beats = [ln for ln in text.splitlines() if "-- heartbeat: no data for" in ln]
    assert 2 <= len(beats) <= 5, text


def test_ALX1544_P125_garbage_pid_file_reads_as_no_pid(tmp_path):
    soak = tmp_path / "soak"
    soak.mkdir()
    for junk in ("garbage", "", "12a"):
        (soak / "serial_logger.pid").write_text(junk)
        info = sl.status(soak)
        assert info["pid"] is None
        assert info["alive"] is False


def test_ALX1544_P126_stale_pid_file_is_cleaned_without_a_kill(tmp_path, monkeypatch):
    soak = tmp_path / "soak"
    soak.mkdir()
    (soak / "serial_logger.pid").write_text("4242")
    killed: list[int] = []
    monkeypatch.setattr(sl, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(sl, "_pid_kill", killed.append)
    assert sl.stop_detached(soak) is False
    assert killed == []
    assert not (soak / "serial_logger.pid").exists()


def test_ALX1544_P127_cli_defaults_and_int_baud(tmp_path, monkeypatch):
    ran: dict[str, object] = {}

    def fake_run(self):
        ran.update(baud=self.baud, hb=self.heartbeat_s, keep=self.retention_days)

    monkeypatch.setattr(SerialLogger, "run", fake_run)
    assert sl.main(["run", "--port", "FAKE1", "--dir", str(tmp_path / "s")]) == 0
    assert ran == {"baud": 115200, "hb": 600.0, "keep": 90}
    with pytest.raises(SystemExit):
        sl.main(["run", "--port", "FAKE1", "--dir", str(tmp_path / "s"), "--baud", "fast"])
