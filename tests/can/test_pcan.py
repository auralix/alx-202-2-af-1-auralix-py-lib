# SPDX-License-Identifier: MIT
"""alx.can.pcan - the PEAK adapter over a scripted PCANBasic (no dongle, no bus).

The fake sits where the vendor DLL sits, so the ctypes marshalling itself is under test: the fake
fills the caller's TPCANMsg and TPCANTimestamp through the same pointers the real library writes.

Proofs (ALX-1553):
  P11 load_driver takes the path, then the environment, then the installed name
  P12 a PCANBasic that will not load raises CanError naming the variable that overrides it
  P13 an unsupported bit rate and an unknown channel are refused before the channel is touched
  P14 a channel that will not open raises CanError carrying the vendor's text
  P15 a received frame is decoded: identifier, payload, flags and the hardware timestamp
  P16 STATUS and ERRFRAME pseudo-frames are counted, never returned as data
  P17 a read failure raises CanError; an empty queue is not a failure
  P18 send marshals identifier, flags and payload; a write failure raises
  P19 recv returns the next frame and None on timeout
  P20 collect returns a window, filters by identifier and stops at limit
  P21 wait_for skips other identifiers and raises when its frame does not come
  P22 flush drops what is queued
  P23 state decodes ok, bus-off and error-active from the status word
  P24 reset clears the controller; a reset failure raises
  P25 close is idempotent, closes the channel and refuses further use
  P26 the bus is a context manager
  P27 the frame trace records opens, traffic and closes; trace=False writes nothing
  P28 _text falls back to the bare code when the vendor cannot describe it
"""

import ctypes
from collections import deque
from functools import partial

import pytest

from alx.can import CanFrame
from alx.can.pcan import (
    ERROR_BUSLIGHT,
    ERROR_BUSOFF,
    ERROR_OK,
    ERROR_QRCVEMPTY,
    MSG_ERRFRAME,
    MSG_EXTENDED,
    MSG_RTR,
    MSG_STANDARD,
    MSG_STATUS,
    TRACE_FILE,
    Pcan,
    load_driver,
)
from alx.errors import CanError

NETINUSE = 0x00800


class FakeDll:
    """A scripted PCANBasic: queue what CAN_Read hands back, record what CAN_Write was given."""

    def __init__(self):
        self.rx: deque[tuple[int, int, int, bytes, int]] = deque()
        self.init_status = ERROR_OK
        self.write_status = ERROR_OK
        self.reset_status = ERROR_OK
        self.status_value = ERROR_OK
        self.errtext_status = ERROR_OK
        self.errtext = "Fake vendor text"
        self.written = []
        self.opened = None
        self.uninit_calls = 0

    def queue(self, can_id=0, data=b"", msgtype=MSG_STANDARD, micros=0, status=ERROR_OK):
        self.rx.append((status, msgtype, can_id, data, micros))

    def CAN_Initialize(self, channel, bitrate, hw_type, io_port, interrupt):
        self.opened = (channel.value, bitrate.value)
        return self.init_status

    def CAN_Uninitialize(self, channel):
        self.uninit_calls += 1
        return ERROR_OK

    def CAN_Reset(self, channel):
        return self.reset_status

    def CAN_GetStatus(self, channel):
        return self.status_value

    def CAN_Read(self, channel, msg, timestamp):
        if not self.rx:
            return ERROR_QRCVEMPTY
        status, msgtype, can_id, data, micros = self.rx.popleft()
        if status != ERROR_OK:
            return status
        entry = msg.contents
        entry.ID = can_id
        entry.MSGTYPE = msgtype
        entry.LEN = len(data)
        for i, byte in enumerate(data):
            entry.DATA[i] = byte
        stamp = timestamp.contents
        millis = micros // 1000
        stamp.millis = millis & 0xFFFFFFFF
        stamp.millis_overflow = millis >> 32
        stamp.micros = micros % 1000
        return ERROR_OK

    def CAN_Write(self, channel, msg):
        entry = msg.contents
        self.written.append((entry.ID, entry.MSGTYPE, bytes(entry.DATA[: entry.LEN])))
        return self.write_status

    def CAN_GetErrorText(self, error, language, buffer):
        if self.errtext_status != ERROR_OK:
            return self.errtext_status
        buffer.value = self.errtext.encode("ascii")
        return ERROR_OK


@pytest.fixture
def dll():
    return FakeDll()


@pytest.fixture
def bus(dll):
    return Pcan(250_000, dll=dll)


# -- the library ------------------------------------------------------------------------


def test_ALX1553_P11_load_driver_prefers_path_then_environment_then_the_installed_name(monkeypatch):
    seen = []

    def fake_windll(name):
        seen.append(name)
        return FakeDll()

    monkeypatch.setattr(ctypes, "WinDLL", fake_windll)
    monkeypatch.delenv("ALX_HIL_PCAN_DLL", raising=False)
    assert isinstance(load_driver(), FakeDll)
    monkeypatch.setenv("ALX_HIL_PCAN_DLL", r"C:\other\PCANBasic.dll")
    load_driver()
    load_driver(r"C:\explicit\PCANBasic.dll")
    assert seen == ["PCANBasic", r"C:\other\PCANBasic.dll", r"C:\explicit\PCANBasic.dll"]


def test_ALX1553_P12_a_library_that_will_not_load_names_the_override_variable(monkeypatch):
    def boom(name):
        raise OSError("not found")

    monkeypatch.setattr(ctypes, "WinDLL", boom)
    monkeypatch.delenv("ALX_HIL_PCAN_DLL", raising=False)
    with pytest.raises(CanError, match=r"cannot load 'PCANBasic'.*ALX_HIL_PCAN_DLL") as excinfo:
        load_driver()
    assert isinstance(excinfo.value.__cause__, OSError), "the OS reason is kept"


# -- opening ----------------------------------------------------------------------------


def test_ALX1553_P13_a_bad_bit_rate_or_channel_is_refused_before_the_dongle_is_touched(dll):
    with pytest.raises(CanError, match=r"bit rate 260000 is not a PCAN nominal rate .*250000"):
        Pcan(260_000, dll=dll)
    with pytest.raises(CanError, match=r"unknown PCAN channel 'usb17' \(known: usb1\.\.usb16\)"):
        Pcan(250_000, channel="usb17", dll=dll)
    assert dll.opened is None, "neither reached CAN_Initialize"


def test_ALX1553_P13_channel_names_are_case_insensitive_and_cover_both_handle_ranges(dll):
    assert Pcan(250_000, channel="USB1", dll=dll).channel == "usb1"
    assert dll.opened[0] == 0x51
    Pcan(250_000, channel="usb16", dll=dll)
    assert dll.opened[0] == 0x510, "channels 9..16 live in the high handle range"
    Pcan(1_000_000, dll=dll)
    assert dll.opened[1] == 0x0014, "the bit rate becomes its BTR0/BTR1 pair"


def test_ALX1553_P14_a_channel_that_will_not_open_carries_the_vendor_text(dll):
    dll.init_status = NETINUSE
    dll.errtext = "The Client Net was already in use"
    with pytest.raises(CanError, match=r"cannot open PCAN usb1 at 250000: .*already in use"):
        Pcan(250_000, dll=dll)


def test_ALX1553_P28_text_falls_back_to_the_bare_code(dll):
    dll.init_status = NETINUSE
    dll.errtext_status = 0x4000
    with pytest.raises(CanError, match=r"cannot open PCAN usb1 at 250000: status 0x800"):
        Pcan(250_000, dll=dll)


# -- receiving --------------------------------------------------------------------------


def test_ALX1553_P15_a_received_frame_is_decoded(bus, dll):
    dll.queue(0x3E8, b"\xa0\xa1\xa2", micros=1_500_000)
    frame = bus.recv()
    assert frame == CanFrame(0x3E8, b"\xa0\xa1\xa2", timestamp_s=1.5)
    dll.queue(0x1ABCDEF, b"\x01", msgtype=MSG_EXTENDED | MSG_RTR, micros=2_000_500)
    frame = bus.recv()
    assert frame.can_id == 0x1ABCDEF
    assert (frame.is_extended, frame.is_remote) == (True, True)
    assert frame.timestamp_s == pytest.approx(2.0005)


def test_ALX1553_P15_the_timestamp_survives_the_millisecond_overflow(bus, dll):
    micros = (2**32 + 7) * 1000 + 123
    dll.queue(0x10, b"", micros=micros)
    assert bus.recv().timestamp_s == pytest.approx(micros / 1_000_000.0)


def test_ALX1553_P16_pseudo_frames_are_counted_not_returned(bus, dll):
    dll.queue(0x0, b"\x01", msgtype=MSG_ERRFRAME)
    dll.queue(0x0, b"\x02", msgtype=MSG_STATUS)
    dll.queue(0x20, b"\x03")
    frame = bus.recv(timeout_s=0.2)
    assert frame.can_id == 0x20, "only the data frame comes through"
    assert (bus.error_frames, bus.status_frames) == (1, 1)


def test_ALX1553_P17_a_read_failure_raises_and_an_empty_queue_does_not(bus, dll):
    assert bus.recv(timeout_s=0.0) is None, "an empty queue is idle, not an error"
    dll.queue(status=ERROR_BUSOFF)
    dll.errtext = "Bus-off"
    with pytest.raises(CanError, match="PCAN usb1 read failed: Bus-off"):
        bus.recv()


# -- sending ----------------------------------------------------------------------------


def test_ALX1553_P18_send_marshals_identifier_flags_and_payload(bus, dll):
    bus.send(CanFrame(0x44C, b"\x11\x22"))
    bus.send(CanFrame(0x1ABCDEF, b"", is_extended=True))
    bus.send(CanFrame(0x7FF, b"", is_remote=True))
    assert dll.written == [
        (0x44C, MSG_STANDARD, b"\x11\x22"),
        (0x1ABCDEF, MSG_EXTENDED, b""),
        (0x7FF, MSG_RTR, b""),
    ]


def test_ALX1553_P18_a_write_failure_raises(bus, dll):
    dll.write_status = 0x00001
    dll.errtext = "Transmit buffer full"
    with pytest.raises(CanError, match="PCAN usb1 write failed: Transmit buffer full"):
        bus.send(CanFrame(0x1))


# -- windows ----------------------------------------------------------------------------


def test_ALX1553_P19_recv_returns_the_next_frame_and_none_on_timeout(bus, dll):
    dll.queue(0x30, b"\x01")
    assert bus.recv(timeout_s=0.5).can_id == 0x30
    assert bus.recv(timeout_s=0.02) is None


def test_ALX1553_P20_collect_returns_a_window(bus, dll):
    for i in range(3):
        dll.queue(0x40 + i, bytes([i]))
    frames = bus.collect(0.05)
    assert [f.can_id for f in frames] == [0x40, 0x41, 0x42]
    assert bus.collect(0.02) == [], "an idle window is empty, not an error"


def test_ALX1553_P20_collect_filters_by_identifier(bus, dll):
    for can_id in (0x50, 0x51, 0x50, 0x52):
        dll.queue(can_id)
    frames = bus.collect(0.05, can_ids=(0x50, 0x52))
    assert [f.can_id for f in frames] == [0x50, 0x50, 0x52]


def test_ALX1553_P20_collect_stops_at_limit(bus, dll):
    for _ in range(5):
        dll.queue(0x60)
    assert len(bus.collect(5.0, limit=2)) == 2, "limit ends the window early"
    assert len(bus.collect(0.05, limit=99)) == 3, "a limit never reached is the whole window"


def test_ALX1553_P21_wait_for_skips_other_identifiers(bus, dll):
    dll.queue(0x70)
    dll.queue(0x71, b"\x09")
    frame = bus.wait_for(0x71, timeout_s=0.5)
    assert frame.data == b"\x09"


def test_ALX1553_P21_wait_for_raises_when_its_frame_does_not_come(bus, dll):
    with pytest.raises(CanError, match=r"no CAN frame 0x080 on usb1 within 0.02 s"):
        bus.wait_for(0x80, timeout_s=0.02)
    dll.queue(0x81)
    with pytest.raises(CanError, match="no CAN frame 0x082"):
        bus.wait_for(0x82, timeout_s=0.0)


def test_ALX1553_P22_flush_drops_what_is_queued(bus, dll):
    dll.queue(0x90)
    dll.queue(0x91)
    bus.flush()
    assert bus.recv(timeout_s=0.0) is None


# -- state ------------------------------------------------------------------------------


def test_ALX1553_P23_state_decodes_the_status_word(bus, dll):
    state = bus.state()
    assert (state.raw, state.is_ok, state.is_bus_off, state.is_error) == (
        ERROR_OK,
        True,
        False,
        False,
    )
    assert "Fake vendor text" in state.text
    dll.status_value = ERROR_BUSOFF
    state = bus.state()
    assert (state.is_ok, state.is_bus_off, state.is_error) == (False, True, True)
    dll.status_value = ERROR_BUSLIGHT
    state = bus.state()
    assert (state.is_ok, state.is_bus_off, state.is_error) == (False, False, True)


def test_ALX1553_P24_reset_clears_the_controller_and_a_failure_raises(bus, dll):
    bus.reset()
    dll.reset_status = 0x02000
    dll.errtext = "Resource not created"
    with pytest.raises(CanError, match="PCAN usb1 reset failed: Resource not created"):
        bus.reset()


# -- lifetime ---------------------------------------------------------------------------


def test_ALX1553_P25_close_is_idempotent_and_refuses_further_use(bus, dll):
    bus.close()
    bus.close()
    assert dll.uninit_calls == 1, "the second close is a no-op"
    for call in (
        partial(bus.send, CanFrame(1)),
        bus.recv,
        partial(bus.collect, 0.01),
        partial(bus.wait_for, 1),
        bus.flush,
        bus.state,
        bus.reset,
    ):
        with pytest.raises(CanError, match="PCAN usb1 is closed"):
            call()


def test_ALX1553_P26_the_bus_is_a_context_manager(dll):
    with Pcan(250_000, dll=dll) as bus:
        assert bus.bitrate == 250_000
    assert dll.uninit_calls == 1


# -- evidence ---------------------------------------------------------------------------


def test_ALX1553_P27_the_frame_trace_records_the_session(tmp_path, dll):
    run_dir = tmp_path / "runs" / "260910"
    with Pcan(250_000, run_dir=run_dir, dll=dll) as bus:
        dll.queue(0x3E8, b"\xa0")
        bus.recv()
        bus.send(CanFrame(0x44C, b"\x01"))
        bus.reset()
    lines = (run_dir / TRACE_FILE).read_text(encoding="ascii").splitlines()
    assert lines == [
        "open usb1 250000 bit/s",
        "rx 0x3E8 [1] -- a0",
        "tx 0x44C [1] -- 01",
        "reset",
        "close usb1",
    ]


def test_ALX1553_P27_trace_off_and_no_run_dir_write_nothing(tmp_path, dll):
    with Pcan(250_000, run_dir=tmp_path, trace=False, dll=dll) as bus:
        bus.send(CanFrame(0x1))
    with Pcan(250_000, dll=dll) as bus:
        bus.send(CanFrame(0x1))
    assert not (tmp_path / TRACE_FILE).exists()
    assert list(tmp_path.iterdir()) == []
