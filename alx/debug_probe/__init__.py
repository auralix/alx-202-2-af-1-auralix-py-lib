# SPDX-License-Identifier: MIT
"""The debug probe as one equipment class: same method names for every tool, tool chosen per bench.

A debug probe (SEGGER J-Link, ST-LINK, ...) is one piece of hardware with several capability groups:
programmer (erase, program, verify), run control (reset, halt, step, registers), memory access while
the core runs, and streaming (SWO, RTT, instruction trace). This package fixes the vocabulary every
adapter implements and opens the adapter the bench is configured for, so a device repo never names a
tool::

    probe = debug_probe.open(mcu="EXAMPLE-MCU", run_dir=RUN_DIR)  # kind from ALX_HIL_DEBUG_PROBE
    probe.program(image, 0x0)                                      # programmer group
    values = probe.read_mem([(0x20000010, 1)])                     # memory group

Contract, implemented by every adapter (``DebugProbe``), by capability group:

* connection: ``kind`` (the tool name the bench selects with ``ALX_HIL_DEBUG_PROBE``), the probe
  serial number, interface and speed as constructor arguments.
* programmer: ``erase_all(hold)`` erases the whole flash; ``erase(start, end, hold, read_back)``
  erases a range and optionally reads 16 bytes back at each address; ``program(image, addr, verify,
  hold, read_back)`` programs a file at an address, optionally verifies and reads back.
* run control: ``reset()``, hardware reset, the core runs afterwards. ``hold=True`` on the
  programmer operations leaves the core halted when the session ends instead of reset + go (tool
  permitting).
* memory: ``read_mem(reads)`` returns ``{addr: bytes}`` for ``[(addr, n), ...]`` in one session;
  ``write_mem(addr, data)`` writes bytes; ``mem_while_running`` declares whether these disturb the
  running core (honestly, per tool).

Reserved names for the groups no adapter implements yet, so a second adapter does not invent a
second vocabulary: run control ``halt()``, ``go()``, ``step()``, ``read_regs()``, ``write_reg()``,
``set_breakpoint()``; programmer ``read_flash()``; streaming ``swo_start()``, ``rtt_open()``. Run
control across several operations needs a persistent probe session (an adapter over the J-Link DLL
or pyOCD), not the one-process-per-operation Commander adapter. Every operation returns a
``ProbeResult`` with the tool transcript and the requested read-backs and raises ``ProbeError`` when
the probe cannot connect, the tool fails, a verify fails or a read-back is missing.

Bench configuration lives in the environment, never in git: ``ALX_HIL_DEBUG_PROBE`` (tool),
``ALX_HIL_DEBUG_PROBE_SN`` (probe serial number when several probes are attached), plus the tool
path variables of the adapters.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from alx.errors import ProbeError

ENV_KIND = "ALX_HIL_DEBUG_PROBE"
ENV_SERIAL = "ALX_HIL_DEBUG_PROBE_SN"
DEFAULT_KIND = "jlink"
KNOWN_KINDS = ("jlink",)


@dataclass
class ProbeResult:
    """What a probe operation returns: the tool transcript and the requested read-backs."""

    transcript: str
    read_back: dict[int, bytes] = field(default_factory=dict)


@runtime_checkable
class DebugProbe(Protocol):
    """The method names every adapter provides; see the module docstring for the semantics."""

    kind: str
    mem_while_running: bool

    def reset(self) -> ProbeResult:
        """Hardware reset; the core runs afterwards."""
        ...

    def erase_all(self, hold: bool = False) -> ProbeResult:
        """Erase the whole flash."""
        ...

    def erase(
        self, start: int, end: int, hold: bool = False, read_back: Iterable[int] = ()
    ) -> ProbeResult:
        """Erase the flash range [start, end]; read 16 bytes back at each ``read_back`` address."""
        ...

    def program(
        self,
        image: str | Path,
        addr: int,
        verify: bool = True,
        hold: bool = False,
        read_back: int = 0,
    ) -> ProbeResult:
        """Program ``image`` at ``addr``; verify by default; read ``read_back`` bytes back."""
        ...

    def read_mem(self, reads: Iterable[tuple[int, int]]) -> dict[int, bytes]:
        """Read every ``(addr, n)`` in one session; return ``{addr: bytes}``."""
        ...

    def write_mem(self, addr: int, data: bytes) -> ProbeResult:
        """Write ``data`` at ``addr``."""
        ...


def open(
    mcu: str,
    run_dir: str | Path,
    kind: str | None = None,
    serial: str | None = None,
    exe: str | Path | None = None,
    **options,
) -> DebugProbe:
    """Open the bench's debug probe, bound to the target MCU and the run directory.

    ``kind`` defaults to ``ALX_HIL_DEBUG_PROBE`` (then ``jlink``), ``serial`` to
    ``ALX_HIL_DEBUG_PROBE_SN``; ``exe`` overrides the adapter's own tool lookup. ``options`` go to
    the adapter (e.g. ``iface``, ``speed_khz``).
    """
    kind = (kind or os.environ.get(ENV_KIND) or DEFAULT_KIND).lower()
    serial = serial or os.environ.get(ENV_SERIAL) or None
    if kind == "jlink":
        from alx.debug_probe.jlink import JLink

        exe_path = Path(exe) if exe else JLink.find_exe()
        if exe_path is None:
            raise ProbeError(
                "JLink.exe not found: install SEGGER J-Link software or set ALX_HIL_JLINK"
            )
        return JLink(exe_path, mcu, run_dir, serial=serial, **options)
    raise ProbeError(f"unknown debug probe kind {kind!r} (known: {', '.join(KNOWN_KINDS)})")
