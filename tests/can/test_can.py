# SPDX-License-Identifier: MIT
"""alx.can - the facade: the frame value type, the bus contract and the bench's choice of dongle.

Proofs (ALX-1553):
  P1  a CanFrame keeps what it was given and derives dlc from the payload
  P2  a frame the wire cannot carry is refused at construction, standard and extended
  P3  str(frame) is the one-line bench-log shape
  P4  open() with no kind opens the PCAN adapter, channel from the environment
  P5  an unknown kind raises CanError naming the known kinds
  P6  explicit kind and channel win over the environment, options reach the adapter
  P7  the PCAN adapter satisfies the CanBus contract; a class lacking a method does not
  P8  periods_ms turns arrival times into inter-arrival milliseconds
  P9  group_by_id splits frames per identifier and keeps arrival order
  P10 BusState carries the raw status word beside the decoded questions
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import alx.can as can
from alx.can import BusState, CanBus, CanFrame, group_by_id, periods_ms
from alx.can.pcan import Pcan
from alx.errors import CanError


class FakeDll:
    """A PCANBasic that answers OK to everything; the scripted one lives in test_pcan.py."""

    def CAN_Initialize(self, channel, bitrate, hw_type, io_port, interrupt):
        return 0

    def CAN_Uninitialize(self, channel):
        return 0

    def CAN_Reset(self, channel):
        return 0

    def CAN_GetStatus(self, channel):
        return 0

    def CAN_Read(self, channel, msg, timestamp):
        return 0x20

    def CAN_Write(self, channel, msg):
        return 0

    def CAN_GetErrorText(self, error, language, buffer):
        return 0


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(can.ENV_KIND, raising=False)
    monkeypatch.delenv(can.ENV_CHANNEL, raising=False)


def test_ALX1553_P1_frame_keeps_its_fields_and_derives_dlc():
    frame = CanFrame(0x3E8, b"\x01\x02\x03")
    assert frame.can_id == 0x3E8
    assert frame.data == b"\x01\x02\x03"
    assert frame.dlc == 3
    assert frame.is_extended is False
    assert frame.is_remote is False
    assert frame.timestamp_s == 0.0
    assert CanFrame(0x7FF).dlc == 0, "a frame with no payload is legal"
    assert CanFrame(0x1FFFFFFF, is_extended=True).can_id == 0x1FFFFFFF
    assert CanFrame(0x1, b"\xff" * 8, timestamp_s=1.5).timestamp_s == 1.5


def test_ALX1553_P2_impossible_frames_are_refused_at_construction():
    with pytest.raises(CanError, match=r"out of range for a standard frame \(0\.\.0x7FF\)"):
        CanFrame(0x800)
    with pytest.raises(CanError, match="out of range for a extended frame"):
        CanFrame(0x20000000, is_extended=True)
    with pytest.raises(CanError, match="out of range"):
        CanFrame(-1)
    with pytest.raises(CanError, match="classic CAN carries at most 8 bytes, got 9"):
        CanFrame(0x10, b"\x00" * 9)
    assert CanFrame(0x800, is_extended=True).can_id == 0x800, "legal as an extended frame"


@given(
    can_id=st.integers(min_value=0, max_value=0x7FF),
    data=st.binary(min_size=0, max_size=8),
)
def test_ALX1553_P2_any_legal_standard_frame_is_accepted(can_id, data):
    frame = CanFrame(can_id, data)
    assert frame.dlc == len(data)


def test_ALX1553_P3_str_is_the_bench_log_line():
    assert str(CanFrame(0x3E8, b"\xa0\xa1")) == "0x3E8 [2] -- a0 a1"
    assert str(CanFrame(0x1, b"", is_extended=True, is_remote=True)) == "0x001 [0] XR"
    assert str(CanFrame(0x7FF, b"\x00")) == "0x7FF [1] -- 00"


def test_ALX1553_P4_open_defaults_to_pcan_and_takes_the_channel_from_the_environment(
    clean_env, monkeypatch
):
    bus = can.open(250_000, dll=FakeDll())
    assert isinstance(bus, Pcan)
    assert bus.kind == "pcan"
    assert bus.channel == "usb1"
    assert bus.bitrate == 250_000
    monkeypatch.setenv(can.ENV_CHANNEL, "usb3")
    monkeypatch.setenv(can.ENV_KIND, "PCAN")
    upper = can.open(500_000, dll=FakeDll())
    assert isinstance(upper, Pcan), "kind is case-insensitive"
    assert upper.channel == "usb3"


def test_ALX1553_P5_unknown_kind_raises_with_the_known_kinds(clean_env, monkeypatch):
    with pytest.raises(CanError, match=r"unknown CAN interface kind 'kvaser' .*known: pcan"):
        can.open(250_000, kind="kvaser")
    monkeypatch.setenv(can.ENV_KIND, "nope")
    with pytest.raises(CanError, match="unknown CAN interface kind 'nope'"):
        can.open(250_000)


def test_ALX1553_P6_explicit_arguments_win_and_options_reach_the_adapter(
    clean_env, tmp_path, monkeypatch
):
    monkeypatch.setenv(can.ENV_KIND, "nope")
    monkeypatch.setenv(can.ENV_CHANNEL, "usb8")
    bus = can.open(
        125_000, run_dir=tmp_path, kind="pcan", channel="usb2", trace=False, dll=FakeDll()
    )
    assert isinstance(bus, Pcan)
    assert bus.channel == "usb2"
    assert bus.bitrate == 125_000


def test_ALX1553_P7_pcan_satisfies_the_contract():
    assert isinstance(Pcan(250_000, dll=FakeDll()), CanBus)

    class Incomplete:
        kind = "x"
        channel = "usb1"
        bitrate = 250_000

        def send(self, frame):
            """Only one method of the contract."""

    assert not isinstance(Incomplete(), CanBus)


def test_ALX1553_P8_periods_ms_are_the_gaps_between_arrivals():
    frames = [CanFrame(1, timestamp_s=t) for t in (1.0, 1.5, 2.0, 2.25)]
    assert periods_ms(frames) == pytest.approx([500.0, 500.0, 250.0])
    assert periods_ms(frames[:1]) == []
    assert periods_ms([]) == []


def test_ALX1553_P9_group_by_id_keeps_arrival_order():
    frames = [
        CanFrame(1, b"\x01", timestamp_s=0.0),
        CanFrame(2, b"\x02", timestamp_s=0.1),
        CanFrame(1, b"\x03", timestamp_s=0.2),
    ]
    grouped = group_by_id(frames)
    assert sorted(grouped) == [1, 2]
    assert [f.data for f in grouped[1]] == [b"\x01", b"\x03"]
    assert [f.data for f in grouped[2]] == [b"\x02"]
    assert group_by_id([]) == {}


def test_ALX1553_P10_bus_state_carries_the_raw_word_and_the_decoded_questions():
    state = BusState(raw=0x10, text="Bus off (0x10)", is_ok=False, is_bus_off=True, is_error=True)
    assert state.raw == 0x10
    assert state.text == "Bus off (0x10)"
    assert (state.is_ok, state.is_bus_off, state.is_error) == (False, True, True)
