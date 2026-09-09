# SPDX-License-Identifier: MIT
"""alx.c_lib.cli - the framed serial CLI client over a scripted wire (no device).

The fake port delivers RX as a queue of chunks (one chunk per read call, so frames can be split at any
byte) and can answer a written line through a responder, like a device would.

Proofs (ALX-1544):
  P20 two pipelined responses in one chunk: read_json returns exactly the first, the second is held for the next read
  P21 trace bytes before the first brace are skipped, logged as RX(trace) and never part of the frame
  P22 braces inside JSON strings (escaped quotes included) do not end the frame
  P23 a pretty (multi-line) response is one frame, ending at the CRLF after the balancing brace
  P24 no response -> read_json returns b"" at the deadline, not later
  P25 read_until_quiet collects everything until the wire stays quiet
  P26 command() records a latency only for an answered command; command_json parses and asserts on silence
  P27 expect_silence returns b"" for a silent wire and the offending bytes otherwise
  P28 set_param / get_params build the c-lib CLI lines (terminator selectable) and decode the answer
  P29 sync sends one bare CR and drains both the held-back bytes and the port
  P30 flush_rx drops held-back bytes and logs them as RX(drop)
  P31 the wire log has one timestamped line per TX / RX / note, in order
  P32 a fresh session has no identity and no latencies
  P33 a frame split across four reads is still one document
  P34 close() closes the wire log
  P35 the typed wrappers (help, reset, id, get, get_param, get_var, get_flag, get_const, get_trig) send the c-lib CLI lines
  P36 set_param accepts str or bytes; get_params is the "data" shortcut of get_param
  P37 trace bytes skipped by read_json accumulate in trace_rx; take_trace hands them to alx.c_lib.trace
  P38 property: any JSON document, preceded by any brace-free trace and split at any byte boundaries,
      is framed exactly once and the trace is kept aside (Hypothesis)
  P39 read_until_quiet returns at total_s when the wire never goes quiet
  P120 mutation-driven hardening: latencies are milliseconds; read_until_quiet logs what it read as RX
"""

import itertools
import json
import re
import tempfile
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from alx.c_lib.cli import Cli
from alx.c_lib.trace import parse_lines
from alx.errors import CliError

OK = b'{"status":"success"}\r\n'


class FakeWire:
    def __init__(self, chunks=(), responder=None):
        self.chunks: list[bytes] = list(chunks)
        self.tx: list[bytes] = []
        self.responder = responder
        self.resets = 0

    def read(self, n: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks[0]
        if len(chunk) <= n:
            return self.chunks.pop(0)
        self.chunks[0] = chunk[n:]
        return chunk[:n]

    def write(self, data: bytes):
        self.tx.append(data)
        if self.responder:
            resp = self.responder(data)
            if resp:
                self.chunks.append(resp)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.resets += 1
        self.chunks.clear()


def device(line: bytes):
    """A minimal c-lib-like device: get-param answers with data, set-param with success, else silence."""
    if line.startswith(b"get-param"):
        return b'{"status":"success","data":{"A_pct":7,"B_en":true}}\r\n'
    if line.startswith((b"set-param", b"get", b"reset", b"id", b"help")):
        return OK
    return None


class Session:
    """session(chunks=(), responder=None) -> (Cli, FakeWire); session.log() -> the wire log text."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.opened: list[Cli] = []

    def __call__(self, chunks=(), responder=None):
        wire = FakeWire(chunks, responder)
        cli = Cli(wire, self.log_path)
        self.opened.append(cli)
        return cli, wire

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8")


@pytest.fixture
def session(tmp_path):
    """Every Cli is closed at teardown (the session owner's duty, here the fixture's)."""
    made = Session(tmp_path / "uart.log")
    yield made
    for cli in made.opened:
        cli.close()


def test_ALX1544_P20_pipelined_responses_are_framed_one_at_a_time(session):
    second = b'{"status":"success","data":{"A_pct":1}}\r\n'
    cli, _wire = session(chunks=[OK + second])
    assert cli.read_json(total_s=0.5) == OK
    assert cli._pending == second
    assert cli.read_json(total_s=0.5) == second
    assert cli.read_json(total_s=0.05) == b""


def test_ALX1544_P21_trace_before_the_frame_is_skipped_and_logged(session):
    cli, _wire = session(chunks=[b"[INF] store written\r\n" + OK])
    assert cli.read_json(total_s=0.5) == OK
    assert re.search(r"RX\(trace\) b'\[INF\] store written", session.log())


def test_ALX1544_P22_braces_inside_strings_do_not_end_the_frame(session):
    doc = b'{"status":"success","data":{"txt":"}{ \\" }"}}\r\n'
    cli, _wire = session(chunks=[doc])
    raw = cli.read_json(total_s=0.5)
    assert raw == doc
    assert json.loads(raw)["data"]["txt"] == '}{ " }'


def test_ALX1544_P23_pretty_response_is_one_frame(session):
    pretty = b'{\r\n    "status":"success",\r\n    "data":\r\n    {\r\n        "A_pct":7\r\n    }\r\n}\r\n'
    cli, _wire = session(responder=lambda line: pretty)
    assert cli.command(b"get\r", total_s=0.5) == pretty
    assert cli.command_json(b"get\r", total_s=0.5) == {"status": "success", "data": {"A_pct": 7}}


def test_ALX1544_P24_silence_returns_empty_at_the_deadline(session):
    cli, _wire = session()
    t0 = time.monotonic()
    assert cli.read_json(total_s=0.1) == b""
    assert 0.05 < time.monotonic() - t0 < 0.6


def test_ALX1544_P25_read_until_quiet_collects_until_the_wire_is_quiet(session):
    cli, _wire = session(chunks=[b"boot ", b"banner\r\n"])
    assert cli.read_until_quiet(total_s=1.0, quiet_s=0.05) == b"boot banner\r\n"
    assert cli.read_until_quiet(total_s=0.2, quiet_s=0.05) == b""


def test_ALX1544_P26_command_latency_only_for_answered_commands(session):
    cli, _wire = session(responder=device)
    assert cli.command(b"get\r", total_s=0.5) == OK
    assert len(cli.latencies_ms) == 1
    assert cli.latencies_ms[0] >= 0.0
    assert cli.command(b"nop\r", total_s=0.05) == b""
    assert len(cli.latencies_ms) == 1
    with pytest.raises(CliError, match=re.escape("no response to b'nop")):
        cli.command_json(b"nop\r", total_s=0.05)


def test_ALX1544_P27_expect_silence_reports_what_came(session):
    cli, _wire = session(responder=device)
    assert cli.expect_silence(b"\r", quiet_s=0.05) == b""
    assert cli.expect_silence(b"get\r", quiet_s=0.05) == OK


def test_ALX1544_P28_set_param_and_get_params_speak_the_cli_protocol(session):
    cli, wire = session(responder=device)
    assert cli.set_param(b"A_pct", b"7") == {"status": "success"}
    assert wire.tx[-1] == b"set-param --key A_pct --val 7\r"
    assert cli.set_param(b"B_en", b"true", term=b"\r\n") == {"status": "success"}
    assert wire.tx[-1] == b"set-param --key B_en --val true\r\n"
    assert cli.get_params() == {"A_pct": 7, "B_en": True}
    assert wire.tx[-1] == b"get-param\r"


def test_ALX1544_P29_sync_sends_a_bare_cr_and_drains_both_ends(session):
    cli, wire = session(chunks=[b'{"a":1}\r\nTAIL'])
    cli.read_json(total_s=0.5)
    assert cli._pending == b"TAIL"
    wire.chunks.append(b"late bytes")
    cli.sync()
    assert wire.tx == [b"\r"]
    assert cli._pending == b""
    assert wire.chunks == []
    assert wire.resets >= 1


def test_ALX1544_P30_flush_rx_drops_held_back_bytes_and_logs_them(session):
    cli, _wire = session(chunks=[b'{"a":1}\r\nTAIL'])
    cli.read_json(total_s=0.5)
    cli.flush_rx()
    assert cli._pending == b""
    assert "RX(drop) b'TAIL'" in session.log()


def test_ALX1544_P31_wire_log_is_a_timestamped_transcript(session):
    cli, _wire = session(responder=device)
    cli.note("session start")
    cli.command(b"get\r", total_s=0.5)
    lines = session.log().splitlines()
    kinds = [re.match(r"^\s*\d+\.\d{3} (TX|RX|RX\(trace\)|RX\(drop\)|--) ", ln) for ln in lines]
    assert all(kinds), lines
    assert [k.group(1) for k in kinds if k is not None] == ["--", "TX", "RX"]
    assert "session start" in lines[0]
    assert "b'get\\r'" in lines[1]


def test_ALX1544_P32_fresh_session_has_no_identity_and_no_latencies(session):
    cli, wire = session()
    assert cli.identity == {}
    assert cli.latencies_ms == []
    assert cli.ser is wire


def test_ALX1544_P33_frame_split_across_reads_is_one_document(session):
    cli, _wire = session(chunks=[b'{"sta', b'tus":"suc', b'cess"}\r', b"\n"])
    assert cli.read_json(total_s=0.5) == OK


def test_ALX1544_P34_close_closes_the_wire_log(session):
    cli, _wire = session()
    cli.close()
    assert cli._log.closed


def test_ALX1544_P35_typed_wrappers_send_the_c_lib_cli_vocabulary(session):
    cli, wire = session(responder=device)
    calls = [
        cli.help,
        cli.reset,
        cli.id,
        cli.get,
        cli.get_param,
        cli.get_var,
        cli.get_flag,
        cli.get_const,
        cli.get_trig,
    ]
    results = [call() for call in calls]
    assert wire.tx == [
        b"help\r",
        b"reset\r",
        b"id\r",
        b"get\r",
        b"get-param\r",
        b"get-var\r",
        b"get-flag\r",
        b"get-const\r",
        b"get-trig\r",
    ]
    assert results[4] == {"status": "success", "data": {"A_pct": 7, "B_en": True}}
    assert all(r["status"] == "success" for r in results)


def test_ALX1544_P36_set_param_accepts_str_or_bytes_and_get_params_is_the_data_shortcut(session):
    cli, wire = session(responder=device)
    assert cli.set_param("A_pct", "7") == {"status": "success"}
    assert wire.tx[-1] == b"set-param --key A_pct --val 7\r"
    assert cli.set_param(b"B_en", "true") == {"status": "success"}
    assert wire.tx[-1] == b"set-param --key B_en --val true\r"
    assert cli.get_params() == cli.get_param()["data"] == {"A_pct": 7, "B_en": True}


def test_ALX1544_P37_trace_bytes_skipped_by_read_json_are_kept_for_the_trace_parser(session):
    first = b"[2000-01-01 00:00:00.103] [INF] AlxParamGroup_CrcOkSame_UsedCopyA\r\n"
    second = b"[2000-01-01 00:00:00.500] [WRN] late\r\n"
    cli, _wire = session(chunks=[first + OK, second + OK])
    assert cli.trace_rx == b""
    assert cli.read_json(total_s=0.5) == OK
    assert cli.read_json(total_s=0.5) == OK
    assert bytes(cli.trace_rx) == first + second, (
        "trace accumulates across frames, frames never enter it"
    )
    cli.flush_rx()
    assert bytes(cli.trace_rx) == first + second, (
        "flush_rx drops port bytes, not the collected trace"
    )
    lines = parse_lines(cli.take_trace())
    assert [(ln.level, ln.text) for ln in lines] == [
        ("INF", "AlxParamGroup_CrcOkSame_UsedCopyA"),
        ("WRN", "late"),
    ]
    assert cli.take_trace() == b""
    assert cli.trace_rx == b""


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(st.characters(codec="ascii"), max_size=8),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(st.characters(codec="ascii"), max_size=5), inner, max_size=3)
    ),
    max_leaves=6,
)


def split_at(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({min(c, len(data)) for c in cuts} | {0, len(data)})
    return [data[a:b] for a, b in itertools.pairwise(points) if b > a]


@settings(deadline=None, max_examples=60)
@given(
    doc=st.dictionaries(st.text(st.characters(codec="ascii"), max_size=5), json_values, max_size=4),
    trace=st.binary(max_size=24).filter(lambda b: b"{" not in b),
    cuts=st.lists(st.integers(min_value=0, max_value=400), max_size=6),
    pretty=st.booleans(),
)
def test_ALX1544_P38_property_any_document_any_trace_any_chunking_is_framed_once(
    doc, trace, cuts, pretty
):
    text = json.dumps(doc, indent=4 if pretty else None).replace("\n", "\r\n")
    frame = text.encode("ascii") + b"\r\n"
    wire = FakeWire(chunks=split_at(trace + frame + b"TAIL", cuts))
    with tempfile.TemporaryDirectory() as tmp:  # not the tmp_path fixture: one dir per example
        cli = Cli(wire, Path(tmp) / "uart.log")
        try:
            raw = cli.read_json(total_s=2.0)
            assert raw == frame
            assert json.loads(raw.replace(b"\r\n", b"")) == doc
            assert bytes(cli.trace_rx) == trace
            assert cli._pending + b"".join(wire.chunks) == b"TAIL"
        finally:
            cli.close()


class ChatteringWire(FakeWire):
    """A wire that never goes quiet."""

    def read(self, n: int) -> bytes:
        return b"x"


def test_ALX1544_P39_read_until_quiet_returns_at_the_deadline_when_the_wire_never_rests(tmp_path):
    cli = Cli(ChatteringWire(), tmp_path / "uart.log")
    try:
        t0 = time.monotonic()
        out = cli.read_until_quiet(total_s=0.2, quiet_s=0.05)
        assert 0.15 < time.monotonic() - t0 < 1.0
        assert out
        assert set(out) == {ord("x")}
    finally:
        cli.close()


def test_ALX1544_P120_latency_is_milliseconds_and_read_until_quiet_logs_rx(session):
    def slow_device(line: bytes):
        time.sleep(0.02)
        return OK

    cli, wire = session(responder=slow_device)
    assert cli.command(b"get\r", total_s=0.5) == OK
    assert 10.0 <= cli.latencies_ms[0] <= 500.0, "milliseconds, not seconds and not a constant"
    wire.chunks.append(b"boot banner\r\n")
    assert cli.read_until_quiet(total_s=1.0, quiet_s=0.05) == b"boot banner\r\n"
    assert "RX b'boot banner" in session.log()
