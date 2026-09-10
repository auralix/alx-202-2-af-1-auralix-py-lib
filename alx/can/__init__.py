# SPDX-License-Identifier: MIT
r"""The CAN interface as one equipment class: one vocabulary, the dongle chosen per bench.

A CAN interface (PEAK PCAN-USB, Kvaser, Vector, ...) is one piece of hardware with three capability
groups: connection (open a channel at a bit rate, close it), traffic (send one frame, receive one
frame, collect a window of frames, wait for one identifier) and state (the controller's bus state
and the queue condition). This package fixes the vocabulary every adapter implements and opens the
adapter the bench is configured for, so a device repo never names a dongle::

    bus = can.open(bitrate=250_000, run_dir=RUN_DIR)  # kind from ALX_HIL_CAN
    frame = bus.wait_for(0x3E8, timeout_s=2.0)  # traffic group
    bus.send(CanFrame(0x44C, b"\x01\x02"))

Contract, implemented by every adapter (``CanBus``), by capability group:

* connection: ``kind`` (the tool name the bench selects with ``ALX_HIL_CAN``), the channel and the
  bit rate as constructor arguments; ``close()``, and the object is a context manager.
* traffic: ``send(frame)`` queues one frame; ``recv(timeout_s)`` returns the next frame or None;
  ``collect(duration_s, ...)`` returns every frame of a window; ``wait_for(can_id, timeout_s)``
  returns the next frame with that identifier; ``flush()`` drops what is already queued.
* state: ``state()`` returns a ``BusState`` - the raw controller status, whether it is bus-off or
  error-passive, and whether any error bit is set. ``reset()`` clears the queues and the state.

A frame is a ``CanFrame``: identifier, payload, the two frame-format flags and, on receive, the
adapter's hardware timestamp in seconds. The timestamp is monotonic within one session and its zero
is the adapter's own, so compare timestamps with each other, never with ``time.time()``.

What this package deliberately does NOT do: decode payloads. A signal layout belongs to the product
that defines it, so decoding lives in the device repository beside the firmware that packs it.

Bench configuration lives in the environment, never in git: ``ALX_HIL_CAN`` (tool),
``ALX_HIL_CAN_CHANNEL`` (which channel of that tool, e.g. ``usb1``), plus the adapter's own library
path variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from alx.errors import CanError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

ENV_KIND = "ALX_HIL_CAN"
ENV_CHANNEL = "ALX_HIL_CAN_CHANNEL"
DEFAULT_KIND = "pcan"
KNOWN_KINDS = ("pcan",)


@dataclass(frozen=True)
class CanFrame:
    """One classic CAN frame: what was on the wire, plus when the adapter saw it.

    ``timestamp_s`` is 0.0 on a frame the caller built for ``send`` and the adapter's hardware time
    on a received one. ``dlc`` is derived from the payload, so a frame cannot claim a length it does
    not carry.
    """

    can_id: int
    data: bytes = b""
    is_extended: bool = False
    is_remote: bool = False
    timestamp_s: float = 0.0

    def __post_init__(self) -> None:
        """Refuse a frame the wire cannot carry, at construction rather than at send."""
        limit = 0x1FFFFFFF if self.is_extended else 0x7FF
        if not 0 <= self.can_id <= limit:
            kind = "extended" if self.is_extended else "standard"
            raise CanError(
                f"CAN id 0x{self.can_id:X} out of range for a {kind} frame (0..0x{limit:X})"
            )
        if len(self.data) > 8:
            raise CanError(f"classic CAN carries at most 8 bytes, got {len(self.data)}")

    @property
    def dlc(self) -> int:
        """The data length code = the payload length."""
        return len(self.data)

    def __str__(self) -> str:
        """One line: identifier, flags and payload, the shape a bench log wants."""
        flags = "".join((("X" if self.is_extended else "-"), ("R" if self.is_remote else "-")))
        return f"0x{self.can_id:03X} [{self.dlc}] {flags} {self.data.hex(' ')}".rstrip()


@dataclass(frozen=True)
class BusState:
    """The controller's view of the bus at one moment.

    ``raw`` is the adapter's own status word, kept so a bench log can show what the tool said;
    ``text`` is the tool's description of it. The three booleans are the questions a test asks.
    """

    raw: int
    text: str
    is_ok: bool
    is_bus_off: bool
    is_error: bool


@runtime_checkable
class CanBus(Protocol):
    """The method names every adapter provides; see the module docstring for the semantics."""

    kind: str
    channel: str
    bitrate: int

    def send(self, frame: CanFrame) -> None:
        """Queue one frame for transmission."""
        ...

    def recv(self, timeout_s: float = 1.0) -> CanFrame | None:
        """Return the next frame, or None when none arrived within ``timeout_s``."""
        ...

    def collect(
        self, duration_s: float, can_ids: Iterable[int] | None = None, limit: int | None = None
    ) -> list[CanFrame]:
        """Return every frame seen for ``duration_s``, optionally only ``can_ids``."""
        ...

    def wait_for(self, can_id: int, timeout_s: float = 2.0) -> CanFrame:
        """Return the next frame with ``can_id``; raise ``CanError`` on timeout."""
        ...

    def flush(self) -> None:
        """Drop whatever is already queued, so the next read sees only new traffic."""
        ...

    def state(self) -> BusState:
        """Return the controller's current bus state."""
        ...

    def reset(self) -> None:
        """Clear the queues and the controller's error state."""
        ...

    def close(self) -> None:
        """Release the channel."""
        ...


def periods_ms(frames: Sequence[CanFrame]) -> list[float]:
    """Inter-arrival times of ``frames`` in milliseconds, in the order given.

    The one piece of arithmetic every cadence test would otherwise repeat. Frames of mixed
    identifiers give the gaps between whatever was passed in, so filter first when a single
    identifier's period is wanted.
    """
    return [
        (frames[i + 1].timestamp_s - frames[i].timestamp_s) * 1000.0 for i in range(len(frames) - 1)
    ]


def group_by_id(frames: Iterable[CanFrame]) -> dict[int, list[CanFrame]]:
    """Split ``frames`` into one list per identifier, each keeping arrival order."""
    grouped: dict[int, list[CanFrame]] = {}
    for frame in frames:
        grouped.setdefault(frame.can_id, []).append(frame)
    return grouped


def open(  # noqa: A001 - the module-level open() of a resource is the stdlib idiom (gzip, shelve, webbrowser)
    bitrate: int,
    run_dir: str | Path | None = None,
    kind: str | None = None,
    channel: str | None = None,
    **options: Any,
) -> CanBus:
    """Open the bench's CAN interface at ``bitrate`` bit/s.

    ``kind`` defaults to ``ALX_HIL_CAN`` (then ``pcan``), ``channel`` to ``ALX_HIL_CAN_CHANNEL``
    (then the adapter's first channel). ``run_dir``, when given, is where the adapter writes its
    frame trace. ``options`` go to the adapter (e.g. ``trace``, ``dll``).
    """
    kind = (kind or os.environ.get(ENV_KIND) or DEFAULT_KIND).lower()
    channel = channel or os.environ.get(ENV_CHANNEL) or None
    if kind == "pcan":
        from alx.can.pcan import Pcan  # noqa: PLC0415 - adapters load on demand

        return Pcan(bitrate, channel=channel, run_dir=run_dir, **options)
    raise CanError(f"unknown CAN interface kind {kind!r} (known: {', '.join(KNOWN_KINDS)})")
