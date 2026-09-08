#*******************************************************************************
# @file			alxJlink.py
# @brief		Auralix Python Library - J-Link Module
# @copyright	Copyright (C) Auralix d.o.o. All rights reserved.
#*******************************************************************************

"""SEGGER J-Link Commander as a bench instrument: hardware reset, flash erase / program / verify and
memory reads of the target, one Commander process per operation.

Every operation writes its script under run_dir (the run's evidence directory) and returns the
Commander transcript. A failed connect or a non-zero exit code raises RuntimeError: an unpowered or
missing target is an error, never a skip. Memory reads (mem8) go through the AHB-AP while the core
RUNS - no halt, no reset - which is what alxRamView builds on.
"""

import os
import re
import subprocess
from pathlib import Path

DEFAULT_EXE_DIR = Path("C:/Program Files/SEGGER")
DEFAULT_EXE_GLOB = "JLink*/JLink.exe"


class JLink:
    def __init__(self, exe, device: str, run_dir, iface: str = "SWD", speed_khz: int = 4000):
        self.exe = Path(exe)
        self.device = device            # J-Link device name of the target MCU (product-owned)
        self.run_dir = Path(run_dir)
        self.iface = iface
        self.speed_khz = speed_khz

    @staticmethod
    def find_exe(env_var: str = "ALX_HIL_JLINK"):
        """JLink.exe from the environment variable, else the newest SEGGER installation; None if absent."""
        env = os.environ.get(env_var)
        if env and Path(env).exists():
            return Path(env)
        cands = sorted(DEFAULT_EXE_DIR.glob(DEFAULT_EXE_GLOB))
        return cands[-1] if cands else None

    # -- one Commander process ------------------------------------------------
    def run(self, lines, script_name: str, timeout_s: float = 60.0) -> str:
        """Write the script lines under run_dir, run J-Link Commander on them, return the transcript.
        Raises RuntimeError when the probe cannot connect or Commander exits non-zero."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        script = self.run_dir / script_name
        script.write_text("\n".join(lines) + "\n", encoding="ascii")
        r = subprocess.run([str(self.exe), "-device", self.device, "-if", self.iface, "-speed", str(self.speed_khz),
                            "-autoconnect", "1", "-NoGui", "1", "-CommanderScript", str(script)],
                           capture_output=True, text=True, timeout=timeout_s)
        if "Cannot connect" in r.stdout or r.returncode != 0:
            raise RuntimeError(f"J-Link {script_name} failed (rc={r.returncode}):\n{r.stdout[-1200:]}")
        return r.stdout

    @staticmethod
    def _go(hold: bool):
        return [] if hold else ["r", "g"]           # hold=True: leave the core halted (next power-on = first boot)

    # -- operations ------------------------------------------------------------
    def reset(self) -> str:
        """Hardware reset + go."""
        return self.run(["connect", "r", "g", "exit"], "reset.jlink", timeout_s=30.0)

    def erase(self, start: int, end: int, hold: bool = False, read_back=()) -> str:
        """Halt, erase the flash range [start, end], optionally read 16 bytes back at each address in
        read_back (verify with parse_mem8 on the transcript), then reset + go unless hold."""
        lines = ["connect", "h", f"erase 0x{start:08X} 0x{end:08X}"]
        lines += [f"mem8 0x{a:08X}, 16" for a in read_back]
        return self.run(lines + self._go(hold) + ["exit"], "erase.jlink")

    def loadbin(self, path, addr: int, hold: bool = False, read_back: int = 0) -> str:
        """Halt, program the file at addr through the flash loader (the containing rows are erased and
        programmed), optionally read read_back (<= 16) bytes back at addr, then reset + go unless hold."""
        p = Path(path).resolve().as_posix()
        lines = ["connect", "h", f'loadbin "{p}",0x{addr:08X}']
        if read_back:
            lines.append(f"mem8 0x{addr:08X}, {read_back}")
        return self.run(lines + self._go(hold) + ["exit"], "loadbin.jlink")

    def flash(self, path, addr: int = 0) -> str:
        """Reset, halt, program + verify the image at addr, reset + go. Raises unless Commander verified it."""
        p = Path(path).resolve().as_posix()
        out = self.run(["connect", "r", "h", f'loadbin "{p}",0x{addr:08X}', f'verifybin "{p}",0x{addr:08X}',
                        "r", "g", "exit"], "flash.jlink", timeout_s=180.0)
        if "Verify successful" not in out:
            raise RuntimeError(f"J-Link flash of {p} not verified:\n{out[-1200:]}")
        return out

    def read_mem8(self, reads, script_name: str = "mem8.jlink") -> dict:
        """reads = iterable of (addr, n) with n <= 16: every location read once in ONE Commander session,
        core not halted. Returns {addr: bytes}. Raises RuntimeError when a read is missing from the transcript."""
        reads = list(reads)
        lines = ["connect"] + [f"mem8 0x{a:08X}, {n}" for a, n in reads] + ["exit"]
        out = self.run(lines, script_name, timeout_s=30.0)
        result = {}
        for a, n in reads:
            raw = self.parse_mem8(out, a)
            if raw is None:
                raise RuntimeError(f"J-Link read at 0x{a:08X} failed:\n{out[-800:]}")
            result[a] = raw[:n]
        return result

    @staticmethod
    def parse_mem8(transcript: str, addr: int):
        """The bytes Commander printed for `mem8 addr, n` (first line, n <= 16), None if absent."""
        m = re.search(rf"^{addr:08X} = ((?:[0-9A-F]{{2}} ?)+)", transcript, re.M)
        if not m:
            return None
        return bytes(int(b, 16) for b in m.group(1).split())
