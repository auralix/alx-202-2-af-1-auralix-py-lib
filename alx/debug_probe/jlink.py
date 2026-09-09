# SPDX-License-Identifier: MIT
"""SEGGER J-Link Commander as a debug-probe adapter (contract: ``alx.debug_probe``).

One J-Link Commander process per operation, driven by a script written under ``run_dir`` so every
script and transcript stays with the run's evidence. Memory reads go through the AHB-AP while the
core runs (no halt, no reset), which is what ``alx.fw.live_watch`` builds on. A failed connect or a
non-zero exit code raises ``ProbeError``: an unpowered or missing target is an error, never a skip.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from alx.debug_probe import ProbeResult
from alx.errors import ProbeError

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)

ENV_EXE = "ALX_HIL_JLINK"
DEFAULT_EXE_DIR = Path("C:/Program Files/SEGGER")
DEFAULT_EXE_GLOB = "JLink*/JLink.exe"
MEM8_RE = r"^{addr:08X} = ((?:[0-9A-F]{{2}} ?)+)"


class JLink:
    """J-Link Commander bound to one probe (optionally by serial), one MCU and one run directory."""

    kind = "jlink"
    mem_while_running = True

    def __init__(
        self,
        exe: str | Path,
        mcu: str,
        run_dir: str | Path,
        serial: str | None = None,
        iface: str = "SWD",
        speed_khz: int = 4000,
    ):
        """Bind Commander at ``exe`` to one probe (``serial``, else the only one attached).

        ``mcu`` is the target name in the tool's vocabulary; every transcript lands in ``run_dir``.
        """
        self.exe = Path(exe)
        self.mcu = mcu
        self.run_dir = Path(run_dir)
        self.serial = serial
        self.iface = iface
        self.speed_khz = speed_khz

    @staticmethod
    def find_exe(env_var: str = ENV_EXE) -> Path | None:
        """Return JLink.exe from the environment, else the newest SEGGER installation, else None."""
        env = os.environ.get(env_var)
        if env and Path(env).exists():
            return Path(env)
        candidates = sorted(DEFAULT_EXE_DIR.glob(DEFAULT_EXE_GLOB))
        return candidates[-1] if candidates else None

    def argv(self, script: Path) -> list[str]:
        """Return the Commander command line for one script."""
        args = [
            str(self.exe),
            "-device",
            self.mcu,
            "-if",
            self.iface,
            "-speed",
            str(self.speed_khz),
        ]
        if self.serial:
            args += ["-SelectEmuBySN", str(self.serial)]
        return [*args, "-autoconnect", "1", "-NoGui", "1", "-CommanderScript", str(script)]

    def run(self, lines: Iterable[str], script_name: str, timeout_s: float = 60.0) -> str:
        """Write the script under run_dir, run Commander on it and return the transcript.

        Raises ``ProbeError`` when the probe cannot connect or Commander exits non-zero.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        script = self.run_dir / script_name
        script.write_text("\n".join(lines) + "\n", encoding="ascii")
        log.debug(
            "J-Link %s: %s", script_name, script.read_text(encoding="ascii").replace("\n", " | ")
        )
        result = subprocess.run(
            self.argv(script), capture_output=True, text=True, timeout=timeout_s, check=False
        )
        if "Cannot connect" in result.stdout or result.returncode != 0:
            raise ProbeError(
                f"J-Link {script_name} failed (rc={result.returncode}):\n{result.stdout[-1200:]}"
            )
        return result.stdout

    @staticmethod
    def _go(hold: bool) -> list[str]:
        return [] if hold else ["r", "g"]

    @staticmethod
    def parse_mem8(transcript: str, addr: int) -> bytes | None:
        """Return the bytes Commander printed for ``mem8 addr, n`` (n <= 16), None if absent."""
        match = re.search(MEM8_RE.format(addr=addr), transcript, re.MULTILINE)
        if not match:
            return None
        return bytes(int(b, 16) for b in match.group(1).split())

    def _read_backs(self, transcript: str, addrs: Iterable[int], what: str) -> dict[int, bytes]:
        out: dict[int, bytes] = {}
        for addr in addrs:
            raw = self.parse_mem8(transcript, addr)
            if raw is None:
                raise ProbeError(
                    f"J-Link {what}: read-back at 0x{addr:08X} missing:\n{transcript[-800:]}"
                )
            out[addr] = raw
        return out

    # -- contract -----------------------------------------------------------------
    def reset(self) -> ProbeResult:
        """Hardware reset, then run."""
        return ProbeResult(self.run(["connect", "r", "g", "exit"], "reset.jlink", timeout_s=30.0))

    def erase_all(self, hold: bool = False) -> ProbeResult:
        """Reset, halt and erase the whole flash, then reset + go unless ``hold``."""
        return ProbeResult(
            self.run(["connect", "r", "h", "erase", *self._go(hold), "exit"], "erase_all.jlink")
        )

    def erase(
        self, start: int, end: int, hold: bool = False, read_back: Iterable[int] = ()
    ) -> ProbeResult:
        """Halt and erase flash [start, end]; read 16 bytes back at each read_back address."""
        read_back = list(read_back)
        lines = ["connect", "h", f"erase 0x{start:08X} 0x{end:08X}"]
        lines += [f"mem8 0x{a:08X}, 16" for a in read_back]
        out = self.run([*lines, *self._go(hold), "exit"], "erase.jlink")
        return ProbeResult(out, self._read_backs(out, read_back, "erase"))

    def program(
        self,
        image: str | Path,
        addr: int,
        verify: bool = True,
        hold: bool = False,
        read_back: int = 0,
    ) -> ProbeResult:
        """Halt and program the file at ``addr`` through the flash loader; verify by default.

        The containing rows are erased and programmed. ``read_back`` (<= 16) bytes at ``addr`` are
        read in the same session, before the reset, so a boot cannot alter what is checked.
        """
        path = Path(image).resolve().as_posix()
        lines = ["connect", "h", f'loadbin "{path}",0x{addr:08X}']
        if verify:
            lines.append(f'verifybin "{path}",0x{addr:08X}')
        if read_back:
            lines.append(f"mem8 0x{addr:08X}, {read_back}")
        out = self.run([*lines, *self._go(hold), "exit"], "program.jlink", timeout_s=180.0)
        if verify and "Verify successful" not in out:
            raise ProbeError(f"J-Link program: {path} not verified:\n{out[-1200:]}")
        backs = self._read_backs(out, [addr], "program") if read_back else {}
        return ProbeResult(out, {a: b[:read_back] for a, b in backs.items()})

    def read_mem(
        self, reads: Iterable[tuple[int, int]], script_name: str = "mem.jlink"
    ) -> dict[int, bytes]:
        """Read every ``(addr, n)``, n <= 16, in one Commander session; the core is not halted."""
        reads = list(reads)
        lines = ["connect", *[f"mem8 0x{a:08X}, {n}" for a, n in reads], "exit"]
        out = self.run(lines, script_name, timeout_s=30.0)
        backs = self._read_backs(out, [a for a, _ in reads], "read_mem")
        return {a: backs[a][:n] for a, n in reads}

    def write_mem(self, addr: int, data: bytes) -> ProbeResult:
        """Write ``data`` byte by byte at ``addr`` and read it back; the core is not halted."""
        lines = ["connect", *[f"w1 0x{addr + i:08X}, 0x{b:02X}" for i, b in enumerate(data)]]
        lines += [f"mem8 0x{addr:08X}, {min(len(data), 16)}", "exit"]
        out = self.run(lines, "write_mem.jlink", timeout_s=30.0)
        got = self._read_backs(out, [addr], "write_mem")[addr][: len(data)]
        if got != data[:16]:
            raise ProbeError(
                f"J-Link write_mem at 0x{addr:08X}: read back {got.hex()} != {data[:16].hex()}"
            )
        return ProbeResult(out, {addr: got})
