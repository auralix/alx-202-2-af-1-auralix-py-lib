# SPDX-License-Identifier: MIT
"""Keysight E36313A commands and transport failures over an injected socket."""

import re
from collections import deque

import pytest

from alx.errors import InstrumentError
from alx.psu.keysight_e36313a import Identity, KeysightE36313A, Limits

IDN = "Keysight Technologies,E36313A,00000000,1.0"
CHANNEL_LIMITS = {
    1: Limits(v_max=6.0, i_max=10.0),
    2: Limits(v_max=25.0, i_max=2.0),
    3: Limits(v_max=25.0, i_max=2.0),
}
MODES = {
    "SYST:PERS:MOD?": "E3631XA",
    "OUTP:PAIR?": "OFF",
    "OUTP:TRAC?": "0",
    "OUTP:COUP:CHAN?": "NONE",
}


class FakeSocket:
    def __init__(self, *, idn=IDN, fragment=4096, scripted=None, modes=None):
        self.idn = idn
        self.fragment = fragment
        self.scripted = {cmd: deque(answers) for cmd, answers in (scripted or {}).items()}
        self.modes = MODES | (modes or {})
        self.channels = {
            1: {"v": 3.3, "i": 0.1, "on": False},
            2: {"v": 12.0, "i": 0.5, "on": False},
            3: {"v": 24.0, "i": 1.0, "on": False},
        }
        self.writes = []
        self.timeouts = []
        self.pending: deque[bytes | Exception] = deque()
        self.closed = False
        self.close_count = 0
        self.ignore_sets = set()
        self.send_error = None
        self.ovp = {1: 6.6, 2: 27.5, 3: 27.5}
        self.ocp = {1: False, 2: False, 3: False}

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, data):
        if self.send_error is not None:
            raise self.send_error
        if self.closed:
            raise OSError("closed fake socket")
        assert data.endswith(b"\n")
        assert b"\r" not in data
        self.writes.append(data)
        cmd = data.decode("ascii").strip()
        if self.scripted.get(cmd):
            self.pending.append(self.scripted[cmd].popleft())
            return
        if cmd == "*IDN?":
            self.pending.append(self.idn.encode("ascii") + b"\n")
            return
        if cmd in self.modes:
            self.pending.append(self.modes[cmd].encode("ascii") + b"\n")
            return
        protection = re.fullmatch(r"(VOLT:PROT|CURR:PROT:STAT)\?\s*\(@([123])\)", cmd)
        if protection:
            name, channel = protection.groups()
            value = self.ovp[int(channel)] if name == "VOLT:PROT" else int(self.ocp[int(channel)])
            self.pending.append(f"{value}\n".encode("ascii"))
            return
        protection_set = re.fullmatch(r"CURR:PROT:STAT ([01]),\s*\(@([123])\)", cmd)
        if protection_set:
            value, channel = protection_set.groups()
            if "OCP" not in self.ignore_sets:
                self.ocp[int(channel)] = bool(int(value))
            return
        query = re.fullmatch(r"(VOLT|CURR|OUTP|MEAS:VOLT|MEAS:CURR)\?\s*\(@([123])\)", cmd)
        if query:
            name, channel = query.groups()
            state = self.channels[int(channel)]
            answer = {
                "VOLT": state["v"],
                "CURR": state["i"],
                "OUTP": int(state["on"]),
                "MEAS:VOLT": state["v"] if state["on"] else 0.0,
                "MEAS:CURR": 0.025 if state["on"] else 0.0,
            }[name]
            self.pending.append(f"{answer}\n".encode("ascii"))
            return
        setting = re.fullmatch(r"(VOLT|CURR|OUTP)\s+([^,]+),\s*\(@([123])\)", cmd)
        if setting:
            name, value, channel = setting.groups()
            if name not in self.ignore_sets:
                key = {"VOLT": "v", "CURR": "i", "OUTP": "on"}[name]
                parsed = bool(int(value)) if name == "OUTP" else float(value)
                self.channels[int(channel)][key] = parsed
            return
        raise AssertionError(f"unexpected command: {cmd!r}")

    def recv(self, size):
        if not self.pending:
            raise TimeoutError("no scripted response")
        data = self.pending.popleft()
        if isinstance(data, Exception):
            raise data
        count = min(size, self.fragment)
        if len(data) > count:
            self.pending.appendleft(data[count:])
        return data[:count]

    def close(self):
        self.closed = True
        self.close_count += 1


class Bench:
    def __init__(self):
        self.opened = []

    def __call__(self, fake=None, **kwargs):
        fake = FakeSocket() if fake is None else fake

        def factory(address, timeout):
            self.opened.append((address, timeout))
            return fake

        return KeysightE36313A("psu.invalid", socket_factory=factory, **kwargs), fake


@pytest.fixture
def bench():
    return Bench()


def commands(fake):
    return [data.decode("ascii").strip() for data in fake.writes]


def mutations(fake):
    return [cmd for cmd in commands(fake) if "?" not in cmd]


@pytest.mark.parametrize("channel", [1, 2, 3])
def test_ALX1753_P41_protection_commands_select_only_requested_channel(bench, channel):
    psu, fake = bench()
    assert psu.overvoltage_limit(channel) == fake.ovp[channel]
    assert not psu.overcurrent_enabled(channel)
    psu.set_overcurrent_enabled(True, channel)
    assert fake.ocp == {c: c == channel for c in (1, 2, 3)}
    assert mutations(fake) == [f"CURR:PROT:STAT 1, (@{channel})"]
    psu.set_overcurrent_enabled(False, channel)
    assert not any(fake.ocp.values())


@pytest.mark.parametrize("value", [0, 1, "ON", None])
def test_ALX1753_P42_protection_requires_boolean_before_mutation(bench, value):
    psu, fake = bench()
    with pytest.raises(InstrumentError):
        psu.set_overcurrent_enabled(value, 2)
    assert mutations(fake) == []


def test_ALX1753_P43_protection_readback_mismatch_fails(bench):
    psu, fake = bench()
    fake.ignore_sets.add("OCP")
    with pytest.raises(InstrumentError, match="not confirmed"):
        psu.set_overcurrent_enabled(True, 2)


def test_ALX1753_P44_invalid_protection_state_is_not_false(bench):
    psu, _ = bench(FakeSocket(scripted={"CURR:PROT:STAT? (@2)": [b"bad\n"]}))
    with pytest.raises(InstrumentError, match="invalid OCP"):
        psu.overcurrent_enabled(2)


def test_ALX1753_P1_constructor_identifies_without_changing_instrument(bench):
    psu, fake = bench()
    assert bench.opened == [(("psu.invalid", 5025), 5.0)]
    assert commands(fake) == ["*IDN?"]
    assert psu.idn == IDN
    assert psu.identity == Identity(IDN, "Keysight Technologies", "E36313A", "00000000", "1.0")
    assert mutations(fake) == []


@pytest.mark.parametrize("model", ["E36312A", "E36313A-extra", "E36313", ""])
def test_ALX1753_P1_wrong_model_is_refused_and_socket_closed(bench, model):
    fake = FakeSocket(idn=f"Keysight Technologies,{model},00000000,1.0")
    with pytest.raises(InstrumentError):
        bench(fake)
    assert fake.closed
    assert mutations(fake) == []


@pytest.mark.parametrize("fragment", [1, 2, 7])
def test_ALX1753_P2_fragmented_lines_are_reassembled(bench, fragment):
    psu, fake = bench(FakeSocket(fragment=fragment), port=15025, timeout_s=1.0)
    assert psu.setpoint_v(channel=2) == 12.0
    assert bench.opened == [(("psu.invalid", 15025), 1.0)]
    assert commands(fake) == ["*IDN?", "VOLT? (@2)"]


@pytest.mark.parametrize("reply", [b"\n", b"\xff\n", b"", b"x" * 4097 + b"\n"])
def test_ALX1753_P3_bad_initial_line_closes_the_connection(bench, reply):
    fake = FakeSocket(scripted={"*IDN?": [reply]})
    with pytest.raises(InstrumentError):
        bench(fake)
    assert fake.closed
    assert commands(fake) == ["*IDN?"]


@pytest.mark.parametrize("failure", [TimeoutError("late"), OSError("broken"), b""])
def test_ALX1753_P4_receive_failure_closes_without_retry_or_late_answer_reuse(bench, failure):
    psu, fake = bench(FakeSocket(scripted={"VOLT? (@1)": [failure]}))
    with pytest.raises(InstrumentError):
        psu.setpoint_v()
    assert fake.closed
    count = len(fake.writes)
    fake.pending.append(b"3.3\n")
    with pytest.raises(InstrumentError):
        psu.setpoint_i()
    assert len(fake.writes) == count
    assert commands(fake).count("VOLT? (@1)") == 1


def test_ALX1753_P4_send_failure_closes_the_connection(bench):
    psu, fake = bench()
    fake.send_error = OSError("broken send")
    with pytest.raises(InstrumentError):
        psu.setpoint_v()
    assert fake.closed


@pytest.mark.parametrize("reply", [b"nan\n", b"inf\n", b"-inf\n", b"garbage\n"])
def test_ALX1753_P5_nonfinite_or_invalid_numeric_response_is_an_error(bench, reply):
    psu, _ = bench(FakeSocket(scripted={"MEAS:VOLT? (@1)": [reply]}))
    with pytest.raises(InstrumentError):
        psu.measure_v()


@pytest.mark.parametrize("channel", [0, 4, -1, True, False, 1.0, "1", None])
def test_ALX1753_P6_invalid_channels_do_not_send_commands(bench, channel):
    psu, fake = bench(limits=CHANNEL_LIMITS)
    before = list(fake.writes)
    for method, args in (
        (psu.setpoint_v, ()),
        (psu.setpoint_i, ()),
        (psu.measure_v, ()),
        (psu.measure_i, ()),
        (psu.output_is_on, ()),
        (psu.set_voltage, (1.0,)),
        (psu.set_current, (0.1,)),
        (psu.output_on, ()),
        (psu.output_off, ()),
    ):
        with pytest.raises(InstrumentError):
            method(*args, channel=channel)
        assert fake.writes == before


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -1.0, "1\nOUTP 1", None]
)
@pytest.mark.parametrize("method", ["set_voltage", "set_current"])
def test_ALX1753_P7_invalid_setpoints_are_rejected_before_io(bench, value, method):
    psu, fake = bench(limits=CHANNEL_LIMITS)
    before = list(fake.writes)
    with pytest.raises(InstrumentError):
        getattr(psu, method)(value)
    assert fake.writes == before


@pytest.mark.parametrize("method", ["set_voltage", "set_current", "output_on"])
def test_ALX1753_P8_mutations_requiring_limits_fail_closed(bench, method):
    psu, fake = bench()
    args = () if method == "output_on" else (1.0,)
    with pytest.raises(InstrumentError):
        getattr(psu, method)(*args)
    assert mutations(fake) == []


@pytest.mark.parametrize("limits", [Limits(v_max=5.0), Limits(i_max=1.0)])
def test_ALX1753_P8_output_enable_requires_voltage_and_current_limits(bench, limits):
    psu, fake = bench(limits={1: limits})
    with pytest.raises(InstrumentError):
        psu.output_on()
    assert mutations(fake) == []


@pytest.mark.parametrize(
    ("method", "channel", "value", "limits"),
    [
        ("set_voltage", 1, 5.1, Limits(v_max=5.0)),
        ("set_current", 1, 0.6, Limits(i_max=0.5)),
        ("set_voltage", 1, 6.1, Limits(v_max=100.0)),
        ("set_current", 1, 10.1, Limits(i_max=100.0)),
        ("set_voltage", 2, 25.1, Limits(v_max=100.0)),
        ("set_current", 2, 2.1, Limits(i_max=100.0)),
        ("set_voltage", 3, 25.1, Limits(v_max=100.0)),
        ("set_current", 3, 2.1, Limits(i_max=100.0)),
    ],
)
def test_ALX1753_P9_declared_limits_and_channel_ratings_bound_writes(
    bench, method, channel, value, limits
):
    psu, fake = bench(limits={channel: limits})
    before = list(fake.writes)
    with pytest.raises(InstrumentError):
        getattr(psu, method)(value, channel=channel)
    assert fake.writes == before


@pytest.mark.parametrize("channel", [1, 2, 3])
def test_ALX1753_P10_setters_verify_only_the_requested_channel(bench, channel):
    psu, fake = bench(limits=CHANNEL_LIMITS)
    original = {key: value.copy() for key, value in fake.channels.items()}
    assert psu.set_voltage(4.0, channel=channel) == 4.0
    assert psu.set_current(0.25, channel=channel) == 0.25
    assert fake.channels[channel]["v"] == 4.0
    assert fake.channels[channel]["i"] == 0.25
    for other in {1, 2, 3} - {channel}:
        assert fake.channels[other] == original[other]
    writes = mutations(fake)
    assert len(writes) == 2
    assert all(cmd.endswith(f"(@{channel})") for cmd in writes)
    assert f"VOLT? (@{channel})" in commands(fake)
    assert f"CURR? (@{channel})" in commands(fake)


@pytest.mark.parametrize(("method", "command"), [("set_voltage", "VOLT"), ("set_current", "CURR")])
def test_ALX1753_P11_setter_readback_mismatch_is_an_error(bench, method, command):
    psu, fake = bench(limits=CHANNEL_LIMITS)
    fake.ignore_sets.add(command)
    with pytest.raises(InstrumentError):
        getattr(psu, method)(1.0)


@pytest.mark.parametrize(
    ("query", "answer"),
    [
        ("SYST:PERS:MOD?", "E3631A"),
        ("OUTP:PAIR?", "SERIES"),
        ("OUTP:PAIR?", "PARALLEL"),
        ("OUTP:TRAC?", "1"),
        ("OUTP:COUP:CHAN?", "CH1,CH2"),
    ],
)
@pytest.mark.parametrize("method", ["set_voltage", "set_current", "output_on", "output_off"])
def test_ALX1753_P12_nonindependent_modes_prevent_every_mutation(bench, query, answer, method):
    psu, fake = bench(FakeSocket(modes={query: answer}), limits=CHANNEL_LIMITS)
    args = (1.0,) if method.startswith("set_") else ()
    with pytest.raises(InstrumentError):
        getattr(psu, method)(*args)
    assert mutations(fake) == []


def test_ALX1753_P12_mode_is_checked_again_before_each_write(bench):
    psu, fake = bench(limits=CHANNEL_LIMITS)
    psu.set_voltage(4.0)
    fake.modes["OUTP:TRAC?"] = "1"
    before = mutations(fake)
    with pytest.raises(InstrumentError):
        psu.set_current(0.2)
    assert mutations(fake) == before


@pytest.mark.parametrize(("key", "value"), [("v", 5.1), ("i", 0.6)])
def test_ALX1753_P13_output_enable_checks_existing_setpoints(bench, key, value):
    psu, fake = bench(limits={1: Limits(v_max=5.0, i_max=0.5)})
    fake.channels[1][key] = value
    with pytest.raises(InstrumentError):
        psu.output_on()
    assert mutations(fake) == []
    assert not fake.channels[1]["on"]


@pytest.mark.parametrize("channel", [1, 2, 3])
def test_ALX1753_P14_outputs_are_confirmed_and_other_channels_stay_unchanged(bench, channel):
    psu, fake = bench(limits=CHANNEL_LIMITS)
    psu.output_on(channel=channel)
    assert psu.output_is_on(channel=channel)
    for other in {1, 2, 3} - {channel}:
        assert not fake.channels[other]["on"]
    psu.output_off(channel=channel)
    assert not psu.output_is_on(channel=channel)
    assert mutations(fake) == [f"OUTP 1, (@{channel})", f"OUTP 0, (@{channel})"]


def test_ALX1753_P14_output_off_does_not_require_limits(bench):
    psu, fake = bench()
    fake.channels[2]["on"] = True
    psu.output_off(channel=2)
    assert not fake.channels[2]["on"]
    assert mutations(fake) == ["OUTP 0, (@2)"]


@pytest.mark.parametrize("answer", [b"2\n", b"-1\n", b"ON\n", b"garbage\n"])
def test_ALX1753_P15_output_state_requires_documented_boolean_answer(bench, answer):
    psu, _ = bench(FakeSocket(scripted={"OUTP? (@1)": [answer]}))
    with pytest.raises(InstrumentError):
        psu.output_is_on()


def test_ALX1753_P15_output_command_readback_mismatch_is_an_error(bench):
    psu, fake = bench(limits=CHANNEL_LIMITS)
    fake.ignore_sets.add("OUTP")
    with pytest.raises(InstrumentError):
        psu.output_on()


def test_ALX1753_P16_state_and_summary_use_selected_channel_without_mutation(bench):
    psu, fake = bench()
    fake.channels[3]["on"] = True
    state = psu.state(channel=3)
    assert state.channel == 3
    assert state.output_on is True
    assert state.setpoint_v == 24.0
    assert state.setpoint_i == 1.0
    assert state.measure_v == 24.0
    assert state.measure_i == 0.025
    summary = psu.bench_summary(channel=3)
    assert "E36313A" in summary
    assert "24" in summary
    assert mutations(fake) == []


def test_ALX1753_P17_close_is_idempotent_and_never_changes_outputs(bench):
    psu, fake = bench()
    fake.channels[1]["on"] = True
    psu.close()
    psu.close()
    assert fake.closed
    assert fake.channels[1]["on"]
    assert mutations(fake) == []
    with pytest.raises(InstrumentError):
        psu.measure_v()


def test_ALX1753_P18_connection_error_is_reported_as_instrument_error():
    def factory(address, timeout):
        raise OSError("unreachable fake instrument")

    with pytest.raises(InstrumentError):
        KeysightE36313A("psu.invalid", socket_factory=factory)


@pytest.mark.parametrize(
    ("method", "query", "requested", "reply", "limits"),
    [
        ("set_voltage", "VOLT? (@1)", 5.0, b"5.001\n", Limits(v_max=5.0)),
        ("set_current", "CURR? (@1)", 0.5, b"0.5004\n", Limits(i_max=0.5)),
        ("set_voltage", "VOLT? (@1)", 0.0, b"-0.001\n", Limits(v_max=5.0)),
        ("set_current", "CURR? (@1)", 0.0, b"-0.0004\n", Limits(i_max=0.5)),
    ],
)
def test_ALX1753_P19_readback_tolerance_does_not_relax_allowed_limits(
    bench, method, query, requested, reply, limits
):
    psu, _ = bench(FakeSocket(scripted={query: [reply]}), limits={1: limits})
    with pytest.raises(InstrumentError):
        getattr(psu, method)(requested)
