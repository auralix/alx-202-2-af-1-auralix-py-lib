# SPDX-License-Identifier: MIT
"""alx.psu.owon_p4603 - the complete OWON P4603 driver over a scripted serial port (no instrument).

The fake implements the documented command set (*IDN?, *RST, MEAS, OUTP, VOLT, CURR, VOLT:LIM,
CURR:LIM with MIN/MAX/DEF) from its state, records every write, can drop answers (the unit's late /
empty replies), answer garbage, and ignore set commands so the verify-and-retry loops are exercised.
It is injected through ``serial_factory``; time.sleep is stubbed and recorded.

Proofs (ALX-1544):
  P1  identity: *IDN? read, parsed into Identity; a foreign instrument closes the port and raises; no write
  P2  the port is opened 8 data bits, configurable baud / parity / stop bits, flow control off, DTR/RTS low
  P3  a query survives tries-1 empty answers and fails after tries
  P4  output_on() refuses when Limits declares neither expect_v nor v_max - nothing is written
  P5  expect_v policy: output_on() refuses a wrong setpoint or an OVP above ovp_max; in-tolerance passes
  P6  output_on() at the declared setpoint writes OUTP ON, confirms with OUTP? and logs under the role name
  P7  output_off() confirms; an unconfirmed switch raises after exactly tries attempts
  P8  the nowait variants write once and never confirm; ON still checks the setpoint first
  P9  query_float retries a non-numeric answer and fails after tries
  P10 power_cycle = confirmed OFF, sleep(off_s), confirmed ON
  P11 wait_output_decay samples MEAS:VOLT? until the output is below the threshold
  P12 state() is one snapshot of eight queries; bench_summary renders it
  P13 with only expect_v declared, the wire never sees anything but queries and OUTP ON / OFF
  P14 ovp_max=None: output_on does not query the OVP limit
  P15 a unit that never answers *IDN? leaves no open port behind
  P16 setters refuse without a declared limit (fail closed) - nothing is written
  P17 set_voltage / set_current write 3-decimal values, verify by read-back; above the limit or the
      hardware rating they refuse
  P18 set_ovp / set_ocp the same against ovp_max / ocp_max
  P19 a setter retries when the read-back does not match and raises after tries
  P20 MIN / MAX / DEF are resolved to numbers and checked against the limits like numbers
  P21 v_max policy (no expect_v): output_on accepts any setpoint up to v_max, refuses above
  P22 factory_reset is refused by default; enabled, it sends *RST and the unit shows factory values
  P23 Identity.parse tolerates a short string
  P170 one retry helper serves every exchange: the wait sits between attempts, never after the last
  P171 _as_float answers None for what is not a number, so no exception is raised inside a loop
"""

import itertools
import logging
import re
import time

import pytest

import alx.psu.owon_p4603 as owon
from alx.errors import InstrumentError
from alx.psu.owon_p4603 import Identity, Limits, OwonP4603, State

IDN = "OWON,P4603,00000000,FV:V1.9.0"
FIXED = Limits(expect_v=24.0, expect_v_tol=0.1, expect_ovp_max=30.0)


class FakeOwon:
    """Scripted P4603 behind a pyserial-like port: state-driven answers, every write recorded."""

    def __init__(
        self,
        idn: str = IDN,
        v=24.0,
        i=3.0,
        ovp=30.0,
        ocp=3.1,
        on=False,
        meas_v=None,
        drop=None,
        scripted=None,
        ignore_sets=(),
    ):
        self.idn, self.v, self.i, self.ovp, self.ocp, self.on = idn, v, i, ovp, ocp, on
        self.meas_v = list(meas_v) if meas_v is not None else None  # scripted MEAS:VOLT? answers
        self.drop = dict(drop or {})  # cmd -> number of empty answers before the real one
        self.scripted = {
            k: list(v) for k, v in (scripted or {}).items()
        }  # cmd -> raw answers first
        self.ignore_sets = tuple(ignore_sets)  # set commands the stubborn unit ignores
        self.writes: list[bytes] = []
        self._rx = b""
        self.is_open = True
        self.dtr = True
        self.rts = True

    def write(self, data: bytes):
        self.writes.append(data)
        cmd = data.decode("ascii").strip()
        if self.drop.get(cmd, 0) > 0:
            self.drop[cmd] -= 1
            self._rx = b""
            return
        if self.scripted.get(cmd):
            self._rx = self.scripted[cmd].pop(0).encode("ascii") + b"\r\n"
            return
        self._rx = self._answer(cmd)

    def _num(self, text, hw_max, default):
        return (
            {"MIN": 0.0, "MAX": hw_max, "DEF": default}.get(text.upper())
            if text.isalpha()
            else float(text)
        )

    def _answer(self, cmd: str) -> bytes:
        if cmd == "*IDN?":
            return self.idn.encode("ascii") + b"\r\n"
        if cmd == "*RST":
            self.v, self.i, self.ovp, self.ocp, self.on = 5.0, 2.0, 60.0, 3.0, False
            return b""
        if cmd == "OUTP?":
            return (b"1" if self.on else b"0") + b"\r\n"
        if cmd == "VOLT?":
            return f"{self.v:.3f}\r\n".encode("ascii")
        if cmd == "CURR?":
            return f"{self.i:.3f}\r\n".encode("ascii")
        if cmd == "VOLT:LIM?":
            return f"{self.ovp:.3f}\r\n".encode("ascii")
        if cmd == "CURR:LIM?":
            return f"{self.ocp:.3f}\r\n".encode("ascii")
        if cmd == "MEAS:VOLT?":
            v = self.meas_v.pop(0) if self.meas_v else (self.v if self.on else 0.0)
            return f"{v:.3f}\r\n".encode("ascii")
        if cmd == "MEAS:CURR?":
            return b"0.012\r\n" if self.on else b"0.000\r\n"
        if cmd == "MEAS:POW?":
            return b"0.288\r\n" if self.on else b"0.000\r\n"
        if cmd in ("OUTP ON", "OUTP OFF"):
            if "OUTP" not in self.ignore_sets:
                self.on = cmd.endswith("ON")
            return b""
        name, _, arg = cmd.partition(" ")
        setters = {
            "VOLT": ("v", 60.0, 5.0),
            "CURR": ("i", 3.0, 2.0),
            "VOLT:LIM": ("ovp", 60.0, 60.0),
            "CURR:LIM": ("ocp", 3.0, 3.0),
        }
        if name in setters and arg:
            if name not in self.ignore_sets:
                attr, hw_max, default = setters[name]
                setattr(self, attr, self._num(arg, hw_max, default))
            return b""  # a set command is silent
        return b""  # unsupported command: no answer (times out)

    def readline(self) -> bytes:
        out, self._rx = self._rx, b""
        return out

    def reset_input_buffer(self):
        self._rx = b""

    def close(self):
        self.is_open = False


def cmds(fake: FakeOwon) -> list[str]:
    return [w.decode("ascii").strip() for w in fake.writes]


def writes_only(fake: FakeOwon) -> list[str]:
    """The non-query commands that hit the wire."""
    return [c for c in cmds(fake) if not c.endswith("?")]


class Bench:
    """bench(fake=None, **driver_kwargs) -> (OwonP4603, FakeOwon); sleeps and the open() kwargs are recorded."""

    def __init__(self):
        self.sleeps: list[float] = []
        self.opened: dict[str, object] = {}

    def __call__(self, fake=None, **kw):
        fake = fake if fake is not None else FakeOwon()

        def factory(port, baud, **kwargs):
            self.opened.clear()
            self.opened.update(port=port, baud=baud, **kwargs)
            return fake

        return OwonP4603("FAKE1", serial_factory=factory, **kw), fake


@pytest.fixture
def bench(monkeypatch):
    made = Bench()
    monkeypatch.setattr(time, "sleep", made.sleeps.append)
    return made


def test_ALX1544_P1_identity_is_read_and_parsed_and_a_foreign_instrument_is_refused(bench):
    psu, fake = bench()
    assert psu.idn == IDN
    assert psu.identity == Identity(IDN, "OWON", "P4603", "00000000", "V1.9.0")
    assert cmds(fake) == ["*IDN?"]
    foreign = FakeOwon(idn="ACME,PSU9000,1,1.0")
    with pytest.raises(InstrumentError, match="not an OWON P4603"):
        bench(foreign)
    assert foreign.is_open is False
    assert writes_only(foreign) == []


def test_ALX1544_P2_port_is_opened_8_data_bits_flow_control_off_dtr_rts_low(bench):
    psu, fake = bench()
    o = bench.opened
    assert o["port"] == "FAKE1"
    assert o["baud"] == 115200
    assert o["bytesize"] == 8
    assert o["parity"] == "N"
    assert o["stopbits"] == 1
    assert o["dsrdtr"] is False
    assert o["rtscts"] is False
    assert o["timeout"] == 5.0
    assert o["write_timeout"] == 5.0
    assert fake.dtr is False
    assert fake.rts is False
    psu.close()
    assert fake.is_open is False
    psu.close()  # idempotent
    bench(FakeOwon(), baud=9600, parity="E", stopbits=2)
    assert bench.opened["baud"] == 9600
    assert bench.opened["parity"] == "E"
    assert bench.opened["stopbits"] == 2


def test_ALX1544_P3_query_survives_empty_answers_up_to_tries(bench):
    psu, fake = bench(FakeOwon(drop={"VOLT?": 2}))
    assert psu.setpoint_v() == 24.0
    assert cmds(fake).count("VOLT?") == 3
    psu, fake = bench(FakeOwon(drop={"CURR?": 3}))
    with pytest.raises(InstrumentError, match="no answer to 'CURR\\?' after 3 tries"):
        psu.setpoint_i()
    assert cmds(fake).count("CURR?") == 3


def test_ALX1544_P4_output_on_refuses_without_any_declared_setpoint_policy(bench):
    psu, fake = bench()  # Limits() = nothing declared
    with pytest.raises(InstrumentError, match="no expect_v or v_max declared"):
        psu.output_on()
    with pytest.raises(InstrumentError, match="no expect_v or v_max declared"):
        psu.output_on_nowait()
    assert writes_only(fake) == []
    assert not fake.on


@pytest.mark.parametrize(
    ("fake_kw", "reason"),
    [
        ({"v": 5.0}, "setpoint is 5.0 V, expected 24.0 V"),
        ({"v": 24.2}, "setpoint is 24.2 V"),
        ({"ovp": 40.0}, "OVP is 40.0 V, above expect_ovp_max 30.0 V"),
    ],
    ids=["wrong-volts", "outside-tolerance", "ovp-too-high"],
)
def test_ALX1544_P5_expect_v_policy_refuses_a_wrong_bench_setting(bench, fake_kw, reason):
    psu, fake = bench(FakeOwon(**fake_kw), limits=FIXED)
    with pytest.raises(InstrumentError, match=reason):
        psu.output_on()
    assert writes_only(fake) == []
    assert not fake.on


def test_ALX1544_P5_expect_v_policy_accepts_a_setpoint_inside_the_tolerance(bench):
    psu, fake = bench(FakeOwon(v=24.05), limits=FIXED)
    psu.output_on()
    assert fake.on


def test_ALX1544_P6_output_on_writes_outp_on_and_confirms(bench, caplog):
    caplog.set_level(logging.INFO, logger="alx.psu.owon_p4603")
    psu, fake = bench(FakeOwon(), limits=FIXED, name="SUPPLY")
    psu.output_on()
    seq = cmds(fake)
    i = seq.index("OUTP ON")
    assert seq[i + 1] == "OUTP?", "the switch is confirmed by reading the output state back"
    assert seq.count("OUTP ON") == 1
    assert fake.on
    assert psu.output_is_on() is True
    assert caplog.messages == ["SUPPLY output ON"]
    assert psu.settle_s in bench.sleeps


def test_ALX1544_P7_output_off_confirms_and_an_unconfirmed_switch_raises_after_tries(bench):
    psu, fake = bench(FakeOwon(on=True))
    psu.output_off()  # OFF needs no declared limits (the safe direction)
    assert not fake.on
    assert psu.output_is_on() is False
    stuck = FakeOwon(on=True, ignore_sets=("OUTP",))
    psu, fake = bench(stuck, tries=4)
    with pytest.raises(InstrumentError, match="OUTP OFF not confirmed"):
        psu.output_off()
    assert cmds(fake).count("OUTP OFF") == 4


def test_ALX1544_P8_nowait_variants_write_once_without_confirm(bench):
    psu, fake = bench(FakeOwon(), limits=FIXED)
    n = len(fake.writes)
    psu.output_on_nowait()
    assert cmds(fake)[-1] == "OUTP ON"
    assert fake.on
    assert "OUTP?" not in cmds(fake)[n:]
    psu.output_off_nowait()
    assert cmds(fake)[-1] == "OUTP OFF"
    assert not fake.on
    wrong = FakeOwon(v=48.0)
    psu, fake = bench(wrong, limits=FIXED)
    with pytest.raises(InstrumentError, match="not touching the output"):
        psu.output_on_nowait()
    assert writes_only(fake) == []


def test_ALX1544_P9_query_float_retries_garbage_and_fails_after_tries(bench):
    psu, fake = bench(FakeOwon(scripted={"VOLT?": ["oops"]}))
    assert psu.setpoint_v() == 24.0
    assert cmds(fake).count("VOLT?") == 2
    psu, fake = bench(FakeOwon(scripted={"MEAS:CURR?": ["a", "b", "c"]}))
    with pytest.raises(InstrumentError, match="no numeric answer"):
        psu.measure_i()


def test_ALX1544_P10_power_cycle_is_confirmed_off_wait_confirmed_on(bench):
    psu, fake = bench(FakeOwon(on=True), limits=FIXED)
    bench.sleeps.clear()
    psu.power_cycle(off_s=1.5)
    seq = cmds(fake)
    assert seq.index("OUTP OFF") < seq.index("OUTP ON")
    assert seq[seq.index("OUTP OFF") + 1] == "OUTP?"
    assert seq[seq.index("OUTP ON") + 1] == "OUTP?"
    assert 1.5 in bench.sleeps
    assert fake.on


def test_ALX1544_P11_wait_output_decay_samples_until_below_threshold(bench):
    psu, fake = bench(FakeOwon(meas_v=[11.8, 6.0, 0.4, 0.0]))
    samples = psu.wait_output_decay(below_v=1.0, timeout_s=5.0)
    assert [v for _, v in samples] == [11.8, 6.0, 0.4]
    assert all(t2 >= t1 for (t1, _), (t2, _) in itertools.pairwise(samples))
    assert cmds(fake).count("MEAS:VOLT?") == 3


def test_ALX1544_P12_state_is_one_snapshot_and_bench_summary_renders_it(bench):
    psu, fake = bench(FakeOwon(on=True))
    n = len(fake.writes)
    s = psu.state()
    assert s == State(True, 24.0, 3.0, 30.0, 3.1, 24.0, 0.012, 0.288)
    assert cmds(fake)[n:] == [
        "OUTP?",
        "VOLT?",
        "CURR?",
        "VOLT:LIM?",
        "CURR:LIM?",
        "MEAS:VOLT?",
        "MEAS:CURR?",
        "MEAS:POW?",
    ]
    assert (
        psu.bench_summary()
        == f"{IDN} set 24.000 V / 3.000 A, OVP 30.0 V, OCP 3.10 A, output ON, meas 24.000 V 0.012 A"
    )


def test_ALX1544_P13_fixed_bench_wire_sees_only_queries_and_outp(bench):
    psu, fake = bench(FakeOwon(), limits=FIXED)
    psu.output_on()
    psu.state()
    psu.output_off_nowait()
    psu.output_on_nowait()
    psu.power_cycle(off_s=0.1)
    psu.wait_output_decay(timeout_s=0.0)
    psu.output_off()
    for what in (
        lambda: psu.set_voltage(12.0),
        lambda: psu.set_current(1.0),
        lambda: psu.set_ovp(20.0),
        lambda: psu.set_ocp(2.0),
        psu.factory_reset,
    ):
        with pytest.raises(InstrumentError, match="refused"):
            what()
    assert len(fake.writes) > 20
    assert set(writes_only(fake)) == {"OUTP ON", "OUTP OFF"}, (
        "a fixed bench never writes a setpoint"
    )


def test_ALX1544_P14_without_ovp_max_output_on_does_not_query_the_limit(bench):
    psu, fake = bench(FakeOwon(ovp=60.0), limits=Limits(expect_v=24.0))
    psu.output_on()
    assert "VOLT:LIM?" not in cmds(fake)
    assert fake.on


def test_ALX1544_P15_a_silent_unit_leaves_no_open_port(bench):
    silent = FakeOwon(drop={"*IDN?": 3})
    with pytest.raises(InstrumentError, match="no answer to '\\*IDN\\?'"):
        bench(silent)
    assert silent.is_open is False


def test_ALX1544_P16_setters_refuse_without_a_declared_limit(bench):
    psu, fake = bench(
        FakeOwon(), limits=Limits(expect_v=24.0)
    )  # no v_max / i_max / ovp_max / ocp_max
    for call, what in (
        (lambda: psu.set_voltage(12.0), "set_voltage"),
        (lambda: psu.set_current(1.0), "set_current"),
        (lambda: psu.set_ovp(20.0), "set_ovp"),
        (lambda: psu.set_ocp(2.0), "set_ocp"),
    ):
        with pytest.raises(InstrumentError, match=f"{what} refused - no limit declared"):
            call()
    assert writes_only(fake) == []
    assert (fake.v, fake.i, fake.ovp, fake.ocp) == (
        24.0,
        3.0,
        30.0,
        3.1,
    )


def test_ALX1544_P17_set_voltage_and_current_verify_and_respect_limit_and_hardware(bench):
    psu, fake = bench(FakeOwon(), limits=Limits(v_max=32.0, i_max=2.5))
    assert psu.set_voltage(12.5) == 12.5
    assert cmds(fake)[-2:] == ["VOLT 12.500", "VOLT?"]
    assert fake.v == 12.5
    assert psu.set_current(0.25) == 0.25
    assert cmds(fake)[-2:] == ["CURR 0.250", "CURR?"]
    assert fake.i == 0.25
    n = len(fake.writes)
    with pytest.raises(InstrumentError, match=re.escape("set_voltage 33 refused - allowed 0..32")):
        psu.set_voltage(33.0)
    with pytest.raises(
        InstrumentError, match=re.escape("set_current 2.6 refused - allowed 0..2.5")
    ):
        psu.set_current(2.6)
    with pytest.raises(InstrumentError, match="set_voltage -1 refused"):
        psu.set_voltage(-1.0)
    assert len(fake.writes) == n, "a refused setter writes nothing"
    psu, fake = bench(FakeOwon(), limits=Limits(v_max=100.0, i_max=10.0))
    with pytest.raises(InstrumentError, match=re.escape("set_voltage 61 refused - allowed 0..60")):
        psu.set_voltage(61.0)
    with pytest.raises(InstrumentError, match=re.escape("set_current 3.5 refused - allowed 0..3")):
        psu.set_current(3.5)


def test_ALX1544_P18_set_ovp_and_ocp_verify_and_respect_their_limits(bench):
    psu, fake = bench(FakeOwon(), limits=Limits(ovp_max=36.0, ocp_max=3.1))
    assert psu.set_ovp(35.0) == 35.0
    assert cmds(fake)[-2:] == ["VOLT:LIM 35.000", "VOLT:LIM?"]
    assert fake.ovp == 35.0
    assert psu.set_ocp(3.05) == 3.05
    assert cmds(fake)[-2:] == ["CURR:LIM 3.050", "CURR:LIM?"]
    assert fake.ocp == 3.05
    with pytest.raises(InstrumentError, match=re.escape("set_ovp 36.5 refused - allowed 0..36")):
        psu.set_ovp(36.5)
    with pytest.raises(InstrumentError, match=re.escape("set_ocp 3.2 refused - allowed 0..3.1")):
        psu.set_ocp(3.2)
    psu, fake = bench(FakeOwon(), limits=Limits(v_max=32.0, ovp_max=36.0))
    psu.output_on()  # sweeping bench: the OVP check uses ovp_max when no expect_ovp_max is declared
    psu, fake = bench(FakeOwon(ovp=40.0), limits=Limits(v_max=32.0, ovp_max=36.0))
    with pytest.raises(InstrumentError, match=re.escape("OVP is 40.0 V, above ovp_max 36.0 V")):
        psu.output_on()


def test_ALX1544_P19_a_setter_retries_on_mismatch_and_raises_after_tries(bench):
    stubborn = FakeOwon(ignore_sets=("VOLT",))
    psu, fake = bench(stubborn, limits=Limits(v_max=32.0), tries=4)
    with pytest.raises(InstrumentError, match=r"VOLT 12.000 not confirmed \(VOLT\? reads 24.0\)"):
        psu.set_voltage(12.0)
    assert cmds(fake).count("VOLT 12.000") == 4
    late = FakeOwon(scripted={"VOLT?": ["24.000"]})  # one stale read-back, then the new value
    psu, fake = bench(late, limits=Limits(v_max=32.0))
    assert psu.set_voltage(12.0) == 12.0
    assert cmds(fake).count("VOLT 12.000") == 2


def test_ALX1544_P20_keywords_resolve_to_numbers_and_are_checked_like_numbers(bench):
    psu, fake = bench(FakeOwon(), limits=Limits(v_max=60.0, i_max=3.0, ovp_max=60.0, ocp_max=3.0))
    assert psu.set_voltage("MIN") == 0.0
    assert cmds(fake)[-2] == "VOLT 0.000"
    assert psu.set_voltage("MAX") == 60.0
    assert cmds(fake)[-2] == "VOLT 60.000"
    assert psu.set_voltage("DEF") == 5.0
    assert cmds(fake)[-2] == "VOLT 5.000"
    assert psu.set_current("def") == 2.0
    assert psu.set_ovp("DEF") == 60.0
    assert psu.set_ocp("MAX") == 3.0
    psu, fake = bench(FakeOwon(), limits=Limits(v_max=32.0))
    with pytest.raises(InstrumentError, match=re.escape("set_voltage 60 refused - allowed 0..32")):
        psu.set_voltage("MAX")
    with pytest.raises(InstrumentError, match="is not a number or MIN/MAX/DEF"):
        psu.set_voltage("HIGH")


def test_ALX1544_P21_v_max_policy_lets_output_on_accept_any_setpoint_up_to_v_max(bench):
    psu, fake = bench(FakeOwon(v=8.0), limits=Limits(v_max=32.0))
    psu.output_on()
    assert fake.on
    psu, fake = bench(FakeOwon(v=33.0), limits=Limits(v_max=32.0))
    with pytest.raises(InstrumentError, match=re.escape("setpoint is 33.0 V, above v_max 32.0 V")):
        psu.output_on()
    assert not fake.on


def test_ALX1544_P22_factory_reset_is_refused_by_default_and_works_when_enabled(bench):
    psu, fake = bench(FakeOwon(v=24.0, ovp=30.0))
    with pytest.raises(InstrumentError, match="factory_reset refused"):
        psu.factory_reset()
    assert "*RST" not in cmds(fake)
    psu, fake = bench(FakeOwon(v=24.0, ovp=30.0, on=True), limits=Limits(factory_reset=True))
    psu.factory_reset()
    assert cmds(fake)[-1] == "*RST"
    assert psu.state() == State(False, 5.0, 2.0, 60.0, 3.0, 0.0, 0.0, 0.0)


def test_ALX1544_P23_identity_parse_tolerates_a_short_string():
    ident = Identity.parse("OWON,P4603")
    assert ident == Identity("OWON,P4603", "OWON", "P4603", "", "")
    assert Identity.parse(IDN).firmware == "V1.9.0"


def test_ALX1544_P128_identity_firmware_field_without_the_fv_prefix_and_short_identities():
    """Mutation-driven hardening: the 4th field is the firmware whether or not it carries FV:."""
    assert Identity.parse("OWON,P4603,123,1.9.0").firmware == "1.9.0"
    assert Identity.parse("OWON,P4603,123").firmware == ""
    assert Identity.parse("OWON,P4603,123").serial_number == "123"


def test_ALX1544_P170_one_retry_helper_waits_between_attempts_never_after_the_last(bench):
    """Four exchanges used to carry their own copy of the loop, each sleeping once too often."""
    psu, fake = bench(FakeOwon(drop={"CURR?": 3}), tries=3, retry_wait_s=0.7)
    bench.sleeps.clear()
    with pytest.raises(InstrumentError, match="no answer"):
        psu.setpoint_i()
    assert cmds(fake).count("CURR?") == 3, "every try is used"
    assert bench.sleeps == [0.7, 0.7], "two waits for three attempts, none after the last"

    # a query that succeeds on the last try waits exactly as often as it failed
    psu, fake = bench(FakeOwon(drop={"VOLT?": 2}), tries=3, retry_wait_s=0.7)
    bench.sleeps.clear()
    assert psu.setpoint_v() == 24.0
    assert bench.sleeps == [0.7, 0.7]

    # the confirmed setter is the same loop: settle before each read, the wait only between tries
    late = FakeOwon(scripted={"VOLT?": ["24.000", "24.000"]})  # two stale read-backs, then 12 V
    psu, fake = bench(late, limits=Limits(v_max=32.0), tries=3, retry_wait_s=0.7, settle_s=0.1)
    bench.sleeps.clear()
    assert psu.set_voltage(12.0) == 12.0
    assert cmds(fake).count("VOLT 12.000") == 3, "three attempts"
    assert bench.sleeps.count(0.1) == 3, "one settle per attempt"
    assert bench.sleeps.count(0.7) == 2, "one wait between attempts, none after the last"


def test_ALX1544_P171_as_float_answers_none_for_what_is_not_a_number():
    """The retry loop asks a question and gets a value or None; it never catches an exception."""
    assert owon._as_float("24.000") == 24.0
    assert owon._as_float("-1e3") == -1000.0
    assert owon._as_float("0") == 0.0, "zero is a value, not a failure"
    assert owon._as_float("") is None
    assert owon._as_float("OVP") is None
    assert owon._as_float("24,0") is None
