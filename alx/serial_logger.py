# SPDX-License-Identifier: MIT
"""Long-term UART logger for the soak mode: timestamped device lines, rotated daily.

The HIL session lasts hours and ``alx.c_lib.cli.Cli`` owns the CLI port while it runs. The soak mode
watches a device for days or weeks with nobody attached: this module owns the port instead and only
records. A COM port has one owner at a time, so the two never run on the same port.

In-process (a fixture on a trace-only UART during a HIL session)::

    logger = SerialLogger("COM5", 115200, log_dir)
    logger.start()
    ...
    logger.stop()

Detached, for days (the soak start of a device repo: start the logger first, then program the
image)::

    python -m alx.serial_logger start --port COM5 --baud 115200 --dir <folder>
    python -m alx.serial_logger status --dir <folder>
    python -m alx.serial_logger stop --dir <folder>

Files under ``<folder>``: ``<folder-name>.log`` rotated at midnight (``retention_days`` files kept)
and ``serial_logger.pid``. Built for a week unattended: the port is reopened when it disappears and
the gap is logged, a heartbeat line every ``heartbeat_s`` of silence tells device silence from
logger death, bytes are decoded with replacement, every line is flushed. The evaluation of the log
is a separate, offline step.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import serial

PID_FILE = "serial_logger.pid"
LINE_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MARK = "-- "


class SerialLogger:
    """Read a serial port line by line and append each line, timestamped, to a rotating log."""

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        log_dir: str | Path = ".",
        name: str | None = None,
        heartbeat_s: float = 600.0,
        reconnect_s: float = 5.0,
        retention_days: int = 90,
        idle_flush_s: float = 2.0,
        serial_factory=serial.Serial,
    ):
        self.port = port
        self.baud = int(baud)
        self.log_dir = Path(log_dir)
        self.name = name or self.log_dir.name or "serial"
        self.heartbeat_s = heartbeat_s
        self.reconnect_s = reconnect_s
        self.retention_days = retention_days
        self.idle_flush_s = idle_flush_s
        self._serial_factory = serial_factory
        self.log_path = self.log_dir / f"{self.name}.log"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ser = None
        self._logger: logging.Logger | None = None
        self.lines = 0
        self.gaps = 0

    # -- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        """Start logging in a background thread; returns at once."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run, name=f"SerialLogger-{self.port}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the background thread and close the port."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    def run(self) -> None:
        """Blocking loop: log until ``stop()`` is called (or the process is killed)."""
        logger = self._open_log()
        logger.info(
            "%sstart: port %s, %d baud, heartbeat %.0f s",
            MARK,
            self.port,
            self.baud,
            self.heartbeat_s,
        )
        buf = b""
        last_rx = last_beat = time.monotonic()
        try:
            while not self._stop.is_set():
                if self._ser is None and not self._open_port(logger):
                    self._stop.wait(self.reconnect_s)
                    continue
                try:
                    chunk = self._ser.read_until(b"\n")
                except (serial.SerialException, OSError) as ex:
                    logger.warning("%sport %s lost: %s", MARK, self.port, ex)
                    self.gaps += 1
                    self._close_port()
                    continue
                now = time.monotonic()
                if chunk:
                    buf += chunk
                    last_rx = last_beat = now
                    if buf.endswith(b"\n"):
                        self._emit(logger, buf)
                        buf = b""
                    continue
                if buf and now - last_rx > self.idle_flush_s:
                    self._emit(logger, buf, partial=True)
                    buf = b""
                if now - last_beat >= self.heartbeat_s:
                    logger.info("%sheartbeat: no data for %.0f s", MARK, now - last_rx)
                    last_beat = now
        finally:
            if buf:
                self._emit(logger, buf, partial=True)
            self._close_port()
            logger.info("%sstop: %d lines, %d port gaps", MARK, self.lines, self.gaps)
            for handler in list(logger.handlers):
                handler.flush()
                handler.close()
                logger.removeHandler(handler)

    # -- internals -----------------------------------------------------------------
    def _open_log(self) -> logging.Logger:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.TimedRotatingFileHandler(
            self.log_path, when="midnight", backupCount=self.retention_days, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(LINE_FORMAT, DATE_FORMAT))
        logger = logging.getLogger(f"{__name__}.{self.name}.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        self._logger = logger
        return logger

    def _open_port(self, logger: logging.Logger) -> bool:
        try:
            self._ser = self._serial_factory(self.port, self.baud, timeout=1.0)
        except (serial.SerialException, OSError) as ex:
            if self.gaps == 0 and self.lines == 0:
                logger.warning(
                    "%sport %s not open: %s (retrying every %.0f s)",
                    MARK,
                    self.port,
                    ex,
                    self.reconnect_s,
                )
                self.gaps += 1
            return False
        logger.info("%sport %s open", MARK, self.port)
        return True

    def _close_port(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001 - closing a dead handle must never raise
                pass
            self._ser = None

    def _emit(self, logger: logging.Logger, raw: bytes, partial: bool = False) -> None:
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        logger.info("%s%s", "(partial) " if partial else "", text)
        self.lines += 1
        for handler in logger.handlers:
            handler.flush()


# -- detached process ------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if platform.system() == "Windows":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return f" {pid} " in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_kill(pid: int) -> None:
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
    else:
        import signal

        os.kill(pid, signal.SIGTERM)


def status(log_dir: str | Path) -> dict:
    """Return pid, alive flag, log path and the last logged line of the logger in ``log_dir``."""
    log_dir = Path(log_dir)
    pid_path = log_dir / PID_FILE
    pid = (
        int(pid_path.read_text().strip())
        if pid_path.exists() and pid_path.read_text().strip().isdigit()
        else None
    )
    log_path = log_dir / f"{log_dir.name}.log"
    last = ""
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        last = lines[-1] if lines else ""
    return {"pid": pid, "alive": bool(pid) and _pid_alive(pid), "log": str(log_path), "last": last}


def stop_detached(log_dir: str | Path) -> bool:
    """Stop the detached logger of ``log_dir`` (via its PID file); True when one was running."""
    log_dir = Path(log_dir)
    info = status(log_dir)
    if info["pid"] and info["alive"]:
        _pid_kill(info["pid"])
    (log_dir / PID_FILE).unlink(missing_ok=True)
    return bool(info["pid"] and info["alive"])


def start_detached(
    port: str, baud: int, log_dir: str | Path, heartbeat_s: float = 600.0, retention_days: int = 90
) -> int:
    """Start ``python -m alx.serial_logger run ...`` detached from this process; returns its PID.

    A logger already running for ``log_dir`` is stopped first (one owner per port). The PID is
    written to ``<log_dir>/serial_logger.pid``; the process is checked alive after one second.
    """
    log_dir = Path(log_dir)
    stop_detached(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable, "-m", "alx.serial_logger", "run",
        "--port", port, "--baud", str(baud), "--dir", str(log_dir),
        "--heartbeat-s", str(heartbeat_s), "--retention-days", str(retention_days),
    ]  # fmt: skip
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    (log_dir / PID_FILE).write_text(str(proc.pid))
    time.sleep(1.0)
    if not _pid_alive(proc.pid):
        (log_dir / PID_FILE).unlink(missing_ok=True)
        raise RuntimeError(
            f"serial logger for {port} died at start; see {log_dir / (log_dir.name + '.log')}"
        )
    return proc.pid


def main(argv: list[str] | None = None) -> int:
    """Command line: start, stop and status manage a detached logger; run is the worker."""
    parser = argparse.ArgumentParser(
        prog="python -m alx.serial_logger", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("start", "run"):
        p = sub.add_parser(cmd)
        p.add_argument("--port", required=True)
        p.add_argument("--baud", type=int, default=115200)
        p.add_argument("--dir", required=True)
        p.add_argument("--heartbeat-s", type=float, default=600.0)
        p.add_argument("--retention-days", type=int, default=90)
    for cmd in ("stop", "status"):
        sub.add_parser(cmd).add_argument("--dir", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "run":
        logger = SerialLogger(
            args.port,
            args.baud,
            args.dir,
            heartbeat_s=args.heartbeat_s,
            retention_days=args.retention_days,
        )
        logger.run()
        return 0
    if args.cmd == "start":
        pid = start_detached(args.port, args.baud, args.dir, args.heartbeat_s, args.retention_days)
        print(f"serial logger started: pid {pid}, {status(args.dir)['log']}")
        return 0
    if args.cmd == "stop":
        print("stopped" if stop_detached(args.dir) else "no logger was running")
        return 0
    info = status(args.dir)
    print(f"pid {info['pid']} alive {info['alive']} log {info['log']}\nlast: {info['last']}")
    return 0 if info["alive"] else 1


if __name__ == "__main__":
    sys.exit(main())
