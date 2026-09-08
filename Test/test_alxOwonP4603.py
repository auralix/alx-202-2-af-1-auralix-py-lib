"""alxOwonP4603 - the OWON P4603 driver over a scripted serial port (no instrument).

The fake answers the SCPI queries from its state, records every write, can drop answers (the unit's
late / empty replies), can answer garbage and can ignore OUTP so the confirm loop is exercised.
time.sleep is stubbed and recorded.

Proofs (ALX-1544):
  P1  identity: *IDN? is read and recorded; a foreign instrument closes the port and raises; no OUTP written
  P2  the port is opened at 115200 with flow control off and DTR/RTS driven low
  P3  a query survives tries-1 empty answers and fails after tries
  P4  output_on() refuses without a declared setpoint - nothing is written
  P5  output_on() refuses a wrong setpoint or an OVP above ovp_max - nothing is written; in-tolerance passes
  P6  output_on() at the declared setpoint writes OUTP ON, confirms with OUTP? and notes it
  P7  output_off() confirms; an unconfirmed switch raises after exactly tries attempts
  P8  the nowait variants write once and never confirm; ON still checks the setpoint first
  P9  query_float retries a non-numeric answer and fails after tries
  P10 power_cycle = confirmed OFF, sleep(off_s), confirmed ON
  P11 wait_output_decay samples MEAS:VOLT? until the output is below the threshold
  P12 bench_summary carries identity, setpoints, limits, output state and measurement
  P13 over a whole scenario the wire never sees anything but queries and OUTP ON / OUTP OFF
  P14 ovp_max=None: the setpoint check does not query the OVP limit
  P15 a unit that never answers *IDN? leaves no open port behind
"""

import pytest

import alxOwonP4603
from alxOwonP4603 import OwonP4603

IDN = "OWON,P4603,00000000,FV:V1.9.0"


class FakeOwon:
    """Scripted P4603 behind a pyserial-like port: state-driven answers, every write recorded."""

    def __init__(self, idn=IDN, v=12.0, i=3.0, ovp=16.0, ocp=3.1, on=False, meas_v=None,
                 drop=None, scripted=None, ignore_outp=False):
        self.idn, self.v, self.i, self.ovp, self.ocp, self.on = idn, v, i, ovp, ocp, on
        self.meas_v = list(meas_v) if meas_v is not None else None    # scripted MEAS:VOLT? answers
        self.drop = dict(drop or {})              # cmd -> number of empty answers before the real one
        self.scripted = {k: list(v) for k, v in (scripted or {}).items()}   # cmd -> raw answers first
        self.ignore_outp = ignore_outp
        self.writes = []
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

    def _answer(self, cmd: str) -> bytes:
        if cmd == "*IDN?":
            return self.idn.encode("ascii") + b"\r\n"
        if cmd == "OUTP?":
            return (b"1" if self.on else b"0") + b"\r\n"
        if cmd == "VOLT?":
            return f"{self.v:.3f}\r\n".encode("ascii")
        if cmd == "CURR?":
            return f"{self.i:.3f}\r\n".encode("ascii")
        if cmd == "VOLT:LIM?":
            return f"{self.ovp:.1f}\r\n".encode("ascii")
        if cmd == "CURR:LIM?":
            return f"{self.ocp:.2f}\r\n".encode("ascii")
        if cmd == "MEAS:VOLT?":
            v = self.meas_v.pop(0) if self.meas_v else (self.v if self.on else 0.0)
            return f"{v:.3f}\r\n".encode("ascii")
        if cmd == "MEAS:CURR?":
            return b"0.012\r\n" if self.on else b"0.000\r\n"
        if cmd == "MEAS:POW?":
            return b"0.144\r\n" if self.on else b"0.000\r\n"
        if cmd in ("OUTP ON", "OUTP OFF"):
            if not self.ignore_outp:
                self.on = cmd.endswith("ON")
            return b""                            # a set command is silent
        return b""                                # unsupported command: no answer (times out)

    def readline(self) -> bytes:
        out, self._rx = self._rx, b""
        return out

    def reset_input_buffer(self):
        self._rx = b""

    def close(self):
        self.is_open = False


def cmds(fake: FakeOwon) -> list:
    return [w.decode("ascii").strip() for w in fake.writes]


@pytest.fixture
def bench(monkeypatch):
    """make(fake=None, **driver_kwargs) -> (OwonP4603, FakeOwon); sleeps and the open() kwargs are recorded."""
    sleeps = []
    opened = {}
    monkeypatch.setattr(alxOwonP4603.time, "sleep", lambda s: sleeps.append(s))

    def make(fake=None, **kw):
        fake = fake if fake is not None else FakeOwon()

        def fake_serial(port, baud, **kwargs):
            opened.clear()
            opened.update(port=port, baud=baud, **kwargs)
            return fake

        monkeypatch.setattr(alxOwonP4603.serial, "Serial", fake_serial)
        return OwonP4603("FAKE1", **kw), fake

    make.sleeps = sleeps
    make.opened = opened
    return make


def test_ALX1544_P1_identity_is_read_and_a_foreign_instrument_is_refused(bench):
    psu, fake = bench()
    assert psu.idn == IDN
    assert cmds(fake) == ["*IDN?"]
    foreign = FakeOwon(idn="ACME,PSU9000,1,1.0")
    with pytest.raises(RuntimeError, match="not an OWON P4603"):
        bench(foreign)
    assert foreign.is_open is False
    assert not [c for c in cmds(foreign) if c.startswith("OUTP ")]


def test_ALX1544_P2_port_is_opened_115200_no_flow_control_dtr_rts_low(bench):
    psu, fake = bench()
    assert bench.opened["port"] == "FAKE1" and bench.opened["baud"] == 115200
    assert bench.opened["dsrdtr"] is False and bench.opened["rtscts"] is False
    assert bench.opened["timeout"] == 5.0 and bench.opened["write_timeout"] == 5.0
    assert fake.dtr is False and fake.rts is False
    psu.close()
    assert fake.is_open is False
    psu.close()                                   # idempotent


def test_ALX1544_P3_query_survives_empty_answers_up_to_tries(bench):
    psu, fake = bench(FakeOwon(drop={"VOLT?": 2}))
    assert psu.setpoint_v() == 12.0
    assert cmds(fake).count("VOLT?") == 3
    psu, fake = bench(FakeOwon(drop={"CURR?": 3}))
    with pytest.raises(RuntimeError, match="no answer to 'CURR\\?' after 3 tries"):
        psu.setpoint_i()
    assert cmds(fake).count("CURR?") == 3


def test_ALX1544_P4_output_on_refuses_without_a_declared_setpoint(bench):
    psu, fake = bench()                           # expect_v not given
    with pytest.raises(RuntimeError, match="no expected setpoint declared"):
        psu.output_on()
    with pytest.raises(RuntimeError, match="no expected setpoint declared"):
        psu.output_on_nowait()
    assert "OUTP ON" not in cmds(fake)
    assert fake.on is False


@pytest.mark.parametrize("fake_kw,reason", [
    (dict(v=5.0), "setpoint is 5.0 V"),
    (dict(v=12.2), "setpoint is 12.2 V"),
    (dict(ovp=20.0), "OVP 20.0 V"),
], ids=["wrong-volts", "outside-tolerance", "ovp-too-high"])
def test_ALX1544_P5_output_on_refuses_a_wrong_bench_setting(bench, fake_kw, reason):
    psu, fake = bench(FakeOwon(**fake_kw), expect_v=12.0, expect_v_tol=0.1, ovp_max=16.5)
    with pytest.raises(RuntimeError, match=reason):
        psu.output_on()
    assert "OUTP ON" not in cmds(fake) and fake.on is False


def test_ALX1544_P5_output_on_accepts_a_setpoint_inside_the_tolerance(bench):
    psu, fake = bench(FakeOwon(v=12.05, ovp=16.5), expect_v=12.0, expect_v_tol=0.1, ovp_max=16.5)
    psu.output_on()
    assert fake.on is True


def test_ALX1544_P6_output_on_writes_outp_on_and_confirms(bench):
    notes = []
    psu, fake = bench(FakeOwon(), expect_v=12.0, ovp_max=16.5, log=notes.append, name="SUPPLY")
    psu.output_on()
    seq = cmds(fake)
    i = seq.index("OUTP ON")
    assert seq[i + 1] == "OUTP?", "the switch is confirmed by reading the output state back"
    assert seq.count("OUTP ON") == 1
    assert fake.on is True and psu.output_is_on() is True
    assert notes == ["SUPPLY output ON"]
    assert psu.settle_s in bench.sleeps


def test_ALX1544_P7_output_off_confirms_and_an_unconfirmed_switch_raises_after_tries(bench):
    psu, fake = bench(FakeOwon(on=True))
    psu.output_off()                              # OFF needs no declared setpoint (the safe direction)
    assert fake.on is False and psu.output_is_on() is False
    stuck = FakeOwon(on=True, ignore_outp=True)
    psu, fake = bench(stuck, tries=4)
    with pytest.raises(RuntimeError, match="OUTP OFF not confirmed"):
        psu.output_off()
    assert cmds(fake).count("OUTP OFF") == 4


def test_ALX1544_P8_nowait_variants_write_once_without_confirm(bench):
    psu, fake = bench(FakeOwon(), expect_v=12.0, ovp_max=16.5)
    n = len(fake.writes)
    psu.output_on_nowait()
    assert cmds(fake)[-1] == "OUTP ON" and fake.on is True
    assert "OUTP?" not in cmds(fake)[n:]
    psu.output_off_nowait()
    assert cmds(fake)[-1] == "OUTP OFF" and fake.on is False
    wrong = FakeOwon(v=24.0)
    psu, fake = bench(wrong, expect_v=12.0, ovp_max=16.5)
    with pytest.raises(RuntimeError, match="not touching the output"):
        psu.output_on_nowait()
    assert "OUTP ON" not in cmds(fake)


def test_ALX1544_P9_query_float_retries_garbage_and_fails_after_tries(bench):
    psu, fake = bench(FakeOwon(scripted={"VOLT?": ["oops"]}))
    assert psu.setpoint_v() == 12.0
    assert cmds(fake).count("VOLT?") == 2
    psu, fake = bench(FakeOwon(scripted={"MEAS:CURR?": ["a", "b", "c"]}))
    with pytest.raises(RuntimeError, match="no numeric answer"):
        psu.measure_i()


def test_ALX1544_P10_power_cycle_is_confirmed_off_wait_confirmed_on(bench):
    psu, fake = bench(FakeOwon(on=True), expect_v=12.0, ovp_max=16.5)
    bench.sleeps.clear()
    psu.power_cycle(off_s=1.5)
    seq = cmds(fake)
    assert seq.index("OUTP OFF") < seq.index("OUTP ON")
    assert seq[seq.index("OUTP OFF") + 1] == "OUTP?" and seq[seq.index("OUTP ON") + 1] == "OUTP?"
    assert 1.5 in bench.sleeps
    assert fake.on is True


def test_ALX1544_P11_wait_output_decay_samples_until_below_threshold(bench):
    psu, fake = bench(FakeOwon(meas_v=[11.8, 6.0, 0.4, 0.0]))
    samples = psu.wait_output_decay(below_v=1.0, timeout_s=5.0)
    assert [v for _, v in samples] == [11.8, 6.0, 0.4]
    assert all(t2 >= t1 for (t1, _), (t2, _) in zip(samples, samples[1:]))
    assert cmds(fake).count("MEAS:VOLT?") == 3


def test_ALX1544_P12_bench_summary_shows_identity_setpoints_limits_state_and_measurement(bench):
    psu, fake = bench(FakeOwon(on=True))
    s = psu.bench_summary()
    assert s == f"{IDN} set 12.000 V / 3.000 A, OVP 16.0 V, OCP 3.10 A, output ON, meas 12.000 V 0.012 A"


def test_ALX1544_P13_the_wire_only_ever_sees_queries_and_outp_on_off(bench):
    psu, fake = bench(FakeOwon(), expect_v=12.0, ovp_max=16.5)
    psu.output_on()
    psu.measure_v(); psu.measure_i(); psu.measure_p(); psu.ocp_a()
    psu.bench_summary()
    psu.output_off_nowait()
    psu.output_on_nowait()
    psu.power_cycle(off_s=0.1)
    psu.wait_output_decay(timeout_s=0.0)
    psu.output_off()
    assert len(fake.writes) > 20
    for c in cmds(fake):
        assert c.endswith("?") or c in ("OUTP ON", "OUTP OFF"), f"unexpected write on the wire: {c!r}"


def test_ALX1544_P14_without_ovp_max_the_setpoint_check_does_not_query_the_limit(bench):
    psu, fake = bench(FakeOwon(ovp=60.0), expect_v=12.0)
    psu.output_on()
    assert "VOLT:LIM?" not in cmds(fake) and fake.on is True


def test_ALX1544_P15_a_silent_unit_leaves_no_open_port(bench):
    silent = FakeOwon(drop={"*IDN?": 3})
    with pytest.raises(RuntimeError, match="no answer to '\\*IDN\\?'"):
        bench(silent)
    assert silent.is_open is False
