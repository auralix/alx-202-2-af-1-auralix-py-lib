# SPDX-License-Identifier: MIT
"""STM32CubeProgrammer CLI as a debug-probe adapter (contract: ``alx.debug_probe``).

The second adapter behind the same vocabulary as ``jlink``: one ``STM32_Programmer_CLI`` process per
operation, every transcript written under ``run_dir`` with the run's evidence, a failed connect or a
non-zero exit raising ``ProbeError`` rather than skipping.

Why a second adapter exists
---------------------------
A probe is two things - the pod on the wire and the flash algorithm driving it - and only the first
is obviously hardware. This adapter can reach the target through the SAME J-Link pod
(``port=JLINK``) while using ST's own loader, so a bench can hold the hardware fixed and change only
the algorithm. That separation is the point: when one tool cannot erase a sector and the other can,
the difference is the loader and nothing else.

What differs from the Commander adapter, and why it matters
-----------------------------------------------------------
* ``erase`` takes a byte RANGE in the contract, because that is what a caller knows. This
  tool erases by SECTOR CODE, so the range is mapped onto sectors through ``sector_map`` -
  a list of
  ``(index, start, length)`` the bench supplies for its part. Without a map the adapter refuses
  rather than guessing, since erasing the wrong sector is not a recoverable mistake.
* reads land in a file (``-r <addr> <size> <file>``) instead of on stdout, so ``read_mem`` reads the
  temporary file back. A read of 16 bytes is still one process.
* ``-w8`` writes a byte at a time with no erase, which is what ``write_mem`` promises.
* there is no "read while the core runs" guarantee here the way Commander's AHB-AP access
  gives one, so ``mem_while_running`` is False and ``alx.fw.live_watch`` will not silently
  pick this adapter up.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from alx.debug_probe import ProbeResult
from alx.errors import ProbeError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

log = logging.getLogger(__name__)

ENV_EXE = "ALX_HIL_CUBEPROG"
DEFAULT_EXE_DIRS = (
    Path("C:/Program Files/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin"),
    Path("C:/Program Files (x86)/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin"),
)
EXE_NAME = "STM32_Programmer_CLI.exe"

# what the tool prints when it could not reach the target; any of these is a failed connect even
# when the exit code is 0, which it sometimes is
CONNECT_FAILURES = (
    "Error: No debug probe detected",
    "Error: No STM32 target found",
    "Error: Unable to connect",
    "Cannot connect",
)


class CubeProg:
    """STM32CubeProgrammer bound to one probe, one MCU and one run directory."""

    kind = "cubeprog"
    mem_while_running = False

    def __init__(
        self,
        exe: str | Path,
        mcu: str,
        run_dir: str | Path,
        serial: str | None = None,
        iface: str = "JLINK",
        speed_khz: int = 4000,
        reset_mode: str = "HWrst",
        sector_map: Sequence[tuple[int, int, int]] | None = None,
    ):
        """Bind the CLI at ``exe`` to one probe, one target and one run directory.

        ``iface`` is the tool's port name - ``JLINK`` to drive a SEGGER pod with ST's loader,
        ``SWD``/``JTAG`` for an ST-LINK. ``mcu`` is kept for parity with the other adapter and for
        the transcripts; this tool identifies the part itself. ``sector_map`` is
        ``[(index, start_addr, length), ...]`` for the part's flash and is what ``erase`` needs.
        """
        self.exe = Path(exe)
        self.mcu = mcu
        self.run_dir = Path(run_dir)
        self.serial = serial
        self.iface = iface
        self.speed_khz = speed_khz
        self.reset_mode = reset_mode
        self.sector_map = list(sector_map or ())

    @staticmethod
    def find_exe(env_var: str = ENV_EXE) -> Path | None:
        """Return the CLI from the environment, else a default installation, else None."""
        env = os.environ.get(env_var)
        if env and Path(env).exists():
            return Path(env)
        for directory in DEFAULT_EXE_DIRS:
            candidate = directory / EXE_NAME
            if candidate.exists():
                return candidate
        return None

    def connect_args(self) -> list[str]:
        """Build the ``-c`` clause: port, speed, reset mode, and the serial when one is given."""
        port = f"port={self.iface}"
        args = ["-c", port, f"freq={self.speed_khz}", f"reset={self.reset_mode}"]
        if self.serial:
            args.append(f"sn={self.serial}")
        return args

    def run(self, args: Sequence[str], what: str, timeout_s: float = 60.0) -> str:
        """Run one CLI invocation, keep the transcript under run_dir, return it.

        Raises ``ProbeError`` when the tool cannot connect or exits non-zero. The connect
        strings are checked separately because the CLI has been known to report a clean exit
        after failing to find a target.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        argv = [str(self.exe), *self.connect_args(), *args]
        log.debug("CubeProgrammer %s: %s", what, " ".join(argv[1:]))
        result = subprocess.run(  # noqa: S603 - argv is the configured CLI and this adapter's own flags
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
        transcript = result.stdout + result.stderr
        (self.run_dir / f"{what}.cubeprog.log").write_text(
            " ".join(argv) + "\n\n" + transcript, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0 or any(bad in transcript for bad in CONNECT_FAILURES):
            raise ProbeError(
                f"CubeProgrammer {what} failed (rc={result.returncode}):\n{transcript[-1200:]}"
            )
        return transcript

    def _sectors_for(self, start: int, end: int) -> list[int]:
        """Return the sectors covering [start, end]; refuse rather than guess without a map."""
        if not self.sector_map:
            raise ProbeError(
                "CubeProgrammer erase: no sector_map for this part. This tool erases by sector "
                "code, so a byte range cannot be mapped without one - pass sector_map="
                "[(index, start, length), ...] when opening the probe, or call erase_all()"
            )
        covered = [i for i, addr, length in self.sector_map if addr < end and addr + length > start]
        if not covered:
            raise ProbeError(
                f"CubeProgrammer erase: 0x{start:08X}..0x{end:08X} covers no sector in the map"
            )
        return covered

    def _read_to_bytes(self, addr: int, size: int, what: str) -> bytes:
        """``-r`` writes what it read into a file; read that back and return the bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "read.bin"
            self.run(["-r", f"0x{addr:08X}", str(size), str(out_file)], what, timeout_s=30.0)
            if not out_file.exists():
                raise ProbeError(
                    f"CubeProgrammer {what}: no file written for the read at 0x{addr:08X}"
                )
            return out_file.read_bytes()[:size]

    # -- contract -----------------------------------------------------------------
    def reset(self) -> ProbeResult:
        """Hardware reset; the core runs afterwards."""
        return ProbeResult(self.run(["-hardRst"], "reset", timeout_s=30.0))

    def erase_all(self, hold: bool = False) -> ProbeResult:
        """Erase the whole flash, then reset + run unless ``hold``."""
        args = ["-e", "all"]
        if not hold:
            args.append("-hardRst")
        return ProbeResult(self.run(args, "erase_all", timeout_s=180.0))

    def erase(
        self, start: int, end: int, hold: bool = False, read_back: Iterable[int] = ()
    ) -> ProbeResult:
        """Erase the sectors covering [start, end]; read 16 bytes back at each read_back address.

        Erasing BY SECTOR is the whole reason this adapter exists: a range erase is what the other
        tool does, and on a dual-bank part it has been seen to report success while leaving the
        first sectors of the second bank programmed.
        """
        read_back = list(read_back)
        sectors = self._sectors_for(start, end)
        args = ["-e", *[str(s) for s in sectors]]
        if not hold:
            args.append("-hardRst")
        out = self.run(args, "erase", timeout_s=180.0)
        backs = {
            addr: self._read_to_bytes(addr, 16, f"erase_read_0x{addr:08X}") for addr in read_back
        }
        return ProbeResult(out, backs)

    def program(
        self,
        image: str | Path,
        addr: int,
        verify: bool = True,
        hold: bool = False,
        read_back: int = 0,
    ) -> ProbeResult:
        """Download ``image`` at ``addr``; verify by default; read ``read_back`` bytes back.

        The read-back happens in a SECOND invocation, after programming and before any reset this
        call performs, so what is checked is what was programmed.
        """
        path = Path(image).resolve()
        if not path.exists():
            raise ProbeError(f"CubeProgrammer program: image not found: {path}")
        args = ["-d", str(path), f"0x{addr:08X}"]
        if verify:
            args.append("-v")
        out = self.run(args, "program", timeout_s=300.0)
        if verify and "Download verified successfully" not in out and "Verification OK" not in out:
            raise ProbeError(f"CubeProgrammer program: {path.name} not verified:\n{out[-1200:]}")
        backs = {}
        if read_back:
            backs[addr] = self._read_to_bytes(addr, read_back, "program_read")
        if not hold:
            self.run(["-hardRst"], "program_reset", timeout_s=30.0)
        return ProbeResult(out, backs)

    def read_mem(self, reads: Iterable[tuple[int, int]]) -> dict[int, bytes]:
        """Read every ``(addr, n)`` and return ``{addr: bytes}``.

        One invocation per address, because this tool reads into a file rather than onto stdout and
        one file per address is clearer than parsing a merged dump.
        """
        return {addr: self._read_to_bytes(addr, n, f"read_mem_0x{addr:08X}") for addr, n in reads}

    def write_mem(self, addr: int, data: bytes) -> ProbeResult:
        """Write ``data`` at ``addr`` with ``-w8`` and read it back; no erase, no reset.

        Byte writes, so this can clear bits in already-programmed flash without erasing a sector -
        which is the only way to invalidate a structure living in flash a tool cannot erase.
        """
        args: list[str] = []
        for i, byte in enumerate(data):
            args += ["-w8", f"0x{addr + i:08X}", f"0x{byte:02X}"]
        out = self.run(args, "write_mem", timeout_s=60.0)
        got = self._read_to_bytes(addr, len(data), "write_mem_read")
        if got != data:
            raise ProbeError(
                f"CubeProgrammer write_mem at 0x{addr:08X}: read back {got.hex()} != {data.hex()}"
            )
        return ProbeResult(out, {addr: got})
