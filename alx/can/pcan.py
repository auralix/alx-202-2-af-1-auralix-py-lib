# SPDX-License-Identifier: MIT
"""PEAK PCAN interface: the CAN bus adapter over the vendor's PCANBasic library.

Source
------
* PCAN-Basic API documentation, PEAK-System Technik GmbH (``PCANBasic.dll``, installed with the
  PCAN device driver package; the header ``PCANBasic.h`` carries the same constants).
* verified on a PCAN-USB FD (IPEH-004022, VID 0x0C72 PID 0x0012) in classic CAN mode at
  250 kbit/s against an STM32 bxCAN node.

The vendor ships a library, not a command line tool, so this adapter binds the four calls a bench
needs through ``ctypes`` rather than adding a runtime dependency: ``CAN_Initialize``,
``CAN_Read``, ``CAN_Write``, ``CAN_GetStatus``, plus ``CAN_Reset``, ``CAN_Uninitialize`` and
``CAN_GetErrorText``. The seam for the offline test suite is the ``Driver`` protocol: the real
``PCANBasic.dll`` and a scripted fake satisfy the same seven signatures.

Classic CAN only
----------------
``CAN_Initialize`` with a ``TPCANBaudrate`` code is the classic-CAN entry point; the FD entry point
is ``CAN_InitializeFD`` with a bit-rate string. An FD dongle in classic mode is what a bxCAN target
needs, and no Auralix target speaks FD today, so ``CanFrame`` is classic: 11 or 29 bit identifier,
at most 8 data bytes. Adding FD later means a second initializer and a longer payload limit, not a
different vocabulary.

Facts the API documentation states but a first user trips over
-------------------------------------------------------------
* ``CAN_Read`` returns ``PCAN_ERROR_QRCVEMPTY`` when the queue is empty. That is the normal idle
  answer, not a failure, so a receive loop polls; there is no blocking read without the Windows
  event object, which would tie the library to one platform's synchronisation primitives.
* the driver also queues STATUS and ERRFRAME pseudo-frames. They carry bus events, not data, so
  they never become a ``CanFrame``; the count is kept and ``state()`` is the way to read the bus.
* a channel is exclusive: a second ``CAN_Initialize`` of the same channel, or PCAN-View holding it,
  answers ``PCAN_ERROR_NETINUSE`` / ``PCAN_ERROR_HWINUSE``.
* the timestamp is the driver's own microsecond clock, wrapping at 2^48 us; its zero is the moment
  the driver loaded, so it compares with other frames of the same session and with nothing else.
* opening a channel puts the dongle on the bus and it ACKs immediately. That matters on a bench
  with a single other node: without an ACK partner a lone transmitter never releases the bus.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import Structure, c_ubyte, c_uint, c_ushort
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from alx.can import BusState, CanFrame
from alx.errors import CanError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import TracebackType

ENV_DLL = "ALX_HIL_PCAN_DLL"
DEFAULT_DLL = "PCANBasic"
TRACE_FILE = "can.log"

# TPCANHandle: the eight low USB channels are 0x51..0x58, channels 9..16 are 0x509..0x510.
CHANNELS: dict[str, int] = {f"usb{i}": 0x50 + i for i in range(1, 9)}
CHANNELS.update({f"usb{i}": 0x500 + i for i in range(9, 17)})
DEFAULT_CHANNEL = "usb1"

# TPCANBaudrate: the BTR0/BTR1 register pair the driver programs for each nominal bit rate.
BITRATES: dict[int, int] = {
    1_000_000: 0x0014,
    800_000: 0x0016,
    500_000: 0x001C,
    250_000: 0x011C,
    125_000: 0x031C,
    100_000: 0x432F,
    95_000: 0xC34E,
    83_000: 0x852B,
    50_000: 0x472F,
    47_000: 0x1414,
    33_000: 0x8B2F,
    20_000: 0x532F,
    10_000: 0x672F,
    5_000: 0x7F7F,
}

# TPCANMessageType
MSG_STANDARD = 0x00
MSG_RTR = 0x01
MSG_EXTENDED = 0x02
MSG_ERRFRAME = 0x40
MSG_STATUS = 0x80

# TPCANStatus, the subset this adapter reasons about.
ERROR_OK = 0x00000
ERROR_BUSLIGHT = 0x00004
ERROR_BUSHEAVY = 0x00008
ERROR_BUSOFF = 0x00010
ERROR_QRCVEMPTY = 0x00020
ERROR_BUSPASSIVE = 0x40000
ERROR_ANYBUSERR = ERROR_BUSLIGHT | ERROR_BUSHEAVY | ERROR_BUSOFF | ERROR_BUSPASSIVE

LANGUAGE_NEUTRAL = 0x09
POLL_S = 0.001


class TPCANMsg(Structure):
    """The vendor's classic-CAN message structure."""

    _fields_ = (
        ("ID", c_uint),
        ("MSGTYPE", c_ubyte),
        ("LEN", c_ubyte),
        ("DATA", c_ubyte * 8),
    )


class TPCANTimestamp(Structure):
    """The vendor's timestamp: milliseconds, their 2^32 overflow count, and microseconds."""

    _fields_ = (
        ("millis", c_uint),
        ("millis_overflow", c_ushort),
        ("micros", c_ushort),
    )


class Driver(Protocol):
    """What this adapter uses of PCANBasic; the real DLL and the test fake both satisfy it.

    The method names are the vendor's exported symbols, so they keep the vendor's casing.
    """

    def CAN_Initialize(  # noqa: N802 - the vendor DLL's exported symbol name
        self, channel: Any, bitrate: Any, hw_type: Any, io_port: Any, interrupt: Any
    ) -> int:
        """Claim the channel and program the classic-CAN bit timing."""
        ...

    def CAN_Uninitialize(self, channel: Any) -> int:  # noqa: N802 - vendor symbol
        """Release the channel."""
        ...

    def CAN_Reset(self, channel: Any) -> int:  # noqa: N802 - vendor symbol
        """Empty the queues and clear the controller's error state."""
        ...

    def CAN_GetStatus(self, channel: Any) -> int:  # noqa: N802 - vendor symbol
        """Return the channel's current status word."""
        ...

    def CAN_Read(self, channel: Any, msg: Any, timestamp: Any) -> int:  # noqa: N802 - vendor symbol
        """Fill ``msg`` and ``timestamp`` from the receive queue."""
        ...

    def CAN_Write(self, channel: Any, msg: Any) -> int:  # noqa: N802 - vendor symbol
        """Put ``msg`` in the transmit queue."""
        ...

    def CAN_GetErrorText(  # noqa: N802 - the vendor DLL's exported symbol name
        self, error: Any, language: Any, buffer: Any
    ) -> int:
        """Fill ``buffer`` with the vendor's description of the status code ``error``."""
        ...


def load_driver(path: str | Path | None = None) -> Driver:
    """Load PCANBasic: ``path``, else ``ALX_HIL_PCAN_DLL``, else the installed ``PCANBasic``."""
    # pragma-guarded: PCANBasic is a Windows DLL and the bench is Windows.
    if sys.platform != "win32":  # pragma: no cover - POSIX has no PCANBasic
        raise CanError("PCANBasic is a Windows library; this adapter needs the Windows PCAN driver")
    name = str(path or os.environ.get(ENV_DLL) or DEFAULT_DLL)
    try:
        return ctypes.WinDLL(name)
    except OSError as exc:
        raise CanError(
            f"cannot load {name!r}: install the PEAK PCAN driver package or set {ENV_DLL}"
        ) from exc


class Pcan:
    """One PCAN channel, opened in the constructor and owned until ``close()``."""

    kind = "pcan"

    def __init__(
        self,
        bitrate: int,
        channel: str | None = None,
        run_dir: str | Path | None = None,
        trace: bool = True,
        dll: Driver | None = None,
    ):
        """Open ``channel`` at ``bitrate`` bit/s; write a frame trace under ``run_dir``.

        ``dll`` replaces the loaded PCANBasic library, which is how the offline suite drives this
        adapter. ``trace`` switches the frame log off for a test that only wants the traffic.
        """
        if bitrate not in BITRATES:
            known = ", ".join(str(b) for b in sorted(BITRATES))
            raise CanError(f"bit rate {bitrate} is not a PCAN nominal rate (known: {known})")
        self.channel = (channel or DEFAULT_CHANNEL).lower()
        if self.channel not in CHANNELS:
            raise CanError(f"unknown PCAN channel {self.channel!r} (known: usb1..usb16)")
        self.bitrate = bitrate
        self.error_frames = 0
        self.status_frames = 0
        self._handle = c_ushort(CHANNELS[self.channel])
        self._dll: Driver = dll if dll is not None else load_driver()
        self._trace_path = Path(run_dir) / TRACE_FILE if (run_dir and trace) else None
        self._closed = False
        status = self._dll.CAN_Initialize(
            self._handle, c_ushort(BITRATES[bitrate]), c_ubyte(0), c_uint(0), c_ushort(0)
        )
        if status != ERROR_OK:
            self._closed = True
            raise CanError(f"cannot open PCAN {self.channel} at {bitrate}: {self._text(status)}")
        self._note(f"open {self.channel} {bitrate} bit/s")

    # -- low level ----------------------------------------------------------------------

    def _text(self, status: int) -> str:
        """Return the vendor's description of a status code, with the code for a bench log."""
        buffer = ctypes.create_string_buffer(256)
        language = c_ushort(LANGUAGE_NEUTRAL)
        if self._dll.CAN_GetErrorText(c_uint(status), language, buffer) != ERROR_OK:
            return f"status 0x{status:X}"
        return f"{buffer.value.decode('latin-1').strip()} (0x{status:X})"

    def _note(self, line: str) -> None:
        """Append one line to the frame trace, when a trace was asked for."""
        if self._trace_path is None:
            return
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self._trace_path.open("a", encoding="ascii") as handle:
            handle.write(f"{line}\n")

    def _require_open(self) -> None:
        """Refuse to use a channel that was already released."""
        if self._closed:
            raise CanError(f"PCAN {self.channel} is closed")

    def _read_one(self) -> CanFrame | None:
        """One ``CAN_Read``: a data frame, or None for an empty queue or a pseudo-frame."""
        msg, stamp = TPCANMsg(), TPCANTimestamp()
        status = self._dll.CAN_Read(self._handle, ctypes.pointer(msg), ctypes.pointer(stamp))
        if status == ERROR_QRCVEMPTY:
            return None
        if status != ERROR_OK:
            raise CanError(f"PCAN {self.channel} read failed: {self._text(status)}")
        if msg.MSGTYPE & MSG_ERRFRAME:
            self.error_frames += 1
            return None
        if msg.MSGTYPE & MSG_STATUS:
            self.status_frames += 1
            return None
        micros = (stamp.millis_overflow << 32 | stamp.millis) * 1000 + stamp.micros
        frame = CanFrame(
            can_id=msg.ID,
            data=bytes(msg.DATA[: msg.LEN]),
            is_extended=bool(msg.MSGTYPE & MSG_EXTENDED),
            is_remote=bool(msg.MSGTYPE & MSG_RTR),
            timestamp_s=micros / 1_000_000.0,
        )
        self._note(f"rx {frame}")
        return frame

    # -- traffic ------------------------------------------------------------------------

    def send(self, frame: CanFrame) -> None:
        """Queue ``frame`` for transmission."""
        self._require_open()
        msg = TPCANMsg()
        msg.ID = frame.can_id
        msg.LEN = frame.dlc
        msg.MSGTYPE = (MSG_EXTENDED if frame.is_extended else MSG_STANDARD) | (
            MSG_RTR if frame.is_remote else 0
        )
        for i, byte in enumerate(frame.data):
            msg.DATA[i] = byte
        status = self._dll.CAN_Write(self._handle, ctypes.pointer(msg))
        if status != ERROR_OK:
            raise CanError(f"PCAN {self.channel} write failed: {self._text(status)}")
        self._note(f"tx {frame}")

    def recv(self, timeout_s: float = 1.0) -> CanFrame | None:
        """Return the next data frame, or None when none arrived within ``timeout_s``."""
        self._require_open()
        deadline = time.monotonic() + timeout_s
        while True:
            frame = self._read_one()
            if frame is not None:
                return frame
            if time.monotonic() >= deadline:
                return None
            time.sleep(POLL_S)

    def collect(
        self, duration_s: float, can_ids: Iterable[int] | None = None, limit: int | None = None
    ) -> list[CanFrame]:
        """Return every data frame seen for ``duration_s``, optionally only ``can_ids``.

        Stops early once ``limit`` frames are collected, so a test that wants three frames of one
        identifier does not pay for the whole window.
        """
        self._require_open()
        wanted = set(can_ids) if can_ids is not None else None
        frames: list[CanFrame] = []
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            frame = self._read_one()
            if frame is None:
                time.sleep(POLL_S)
                continue
            if wanted is not None and frame.can_id not in wanted:
                continue
            frames.append(frame)
            if limit is not None and len(frames) >= limit:
                break
        return frames

    def wait_for(self, can_id: int, timeout_s: float = 2.0) -> CanFrame:
        """Return the next frame with ``can_id``; raise ``CanError`` when it does not come."""
        self._require_open()
        deadline = time.monotonic() + timeout_s
        while True:
            frame = self._read_one()
            if frame is not None and frame.can_id == can_id:
                return frame
            if time.monotonic() >= deadline:
                raise CanError(
                    f"no CAN frame 0x{can_id:03X} on {self.channel} within {timeout_s:g} s"
                )
            if frame is None:
                time.sleep(POLL_S)

    def flush(self) -> None:
        """Drop whatever is already queued, so the next read sees only new traffic."""
        self._require_open()
        while self._read_one() is not None:
            pass

    # -- state --------------------------------------------------------------------------

    def state(self) -> BusState:
        """Return the controller's current bus state."""
        self._require_open()
        raw = self._dll.CAN_GetStatus(self._handle)
        return BusState(
            raw=raw,
            text=self._text(raw),
            is_ok=raw == ERROR_OK,
            is_bus_off=bool(raw & ERROR_BUSOFF),
            is_error=bool(raw & ERROR_ANYBUSERR),
        )

    def reset(self) -> None:
        """Clear the queues and the controller's error state."""
        self._require_open()
        status = self._dll.CAN_Reset(self._handle)
        if status != ERROR_OK:
            raise CanError(f"PCAN {self.channel} reset failed: {self._text(status)}")
        self._note("reset")

    # -- lifetime -----------------------------------------------------------------------

    def close(self) -> None:
        """Release the channel; closing twice is not an error."""
        if self._closed:
            return
        self._closed = True
        self._dll.CAN_Uninitialize(self._handle)
        self._note(f"close {self.channel}")

    def __enter__(self) -> Pcan:
        """Return the open bus, so a test can scope it with ``with``."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the channel whatever ended the block."""
        self.close()
