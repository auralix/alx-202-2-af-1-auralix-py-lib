# SPDX-License-Identifier: MIT
"""alx.c_lib.trace - parsers of the C library trace output (no device).

Proofs (ALX-1544):
  P60 parse_banner extracts name, version, bin and the 7-char build hash from a boot transcript; {} without one
  P65 parse_line splits one [timestamp] [LEVEL] text line and returns None for anything else
  P66 parse_lines keeps only the trace lines of a mixed transcript (banner, JSON frame, noise)
"""

from hypothesis import given
from hypothesis import strategies as st

from alx.c_lib.trace import TraceLine, parse_banner, parse_line, parse_lines

BANNER = (
    b"[2000-01-01 00:00:00.028] [INF] APP START\r\n"
    b"[2000-01-01 00:00:00.065] [INF] Example Device FW Started:\r\n"
    b"[2000-01-01 00:00:00.072] [INF] - FW Name: ExampleDeviceFw\r\n"
    b"[2000-01-01 00:00:00.079] [INF] - FW Version: 1.2.3.2609081200.0123456789abcdef0123456789abcdef01234567\r\n"
    b"[2000-01-01 00:00:00.089] [INF] - FW Bin: 2609081200_EX-1_ExampleDeviceFw_V1-2-3_0123456.bin\r\n"
    b"[2000-01-01 00:00:00.100] [INF] \r\n"
)


def test_ALX1544_P60_parse_banner_extracts_the_image_identity():
    ident = parse_banner(BANNER)
    assert ident == {
        "name": "ExampleDeviceFw",
        "ver": "1.2.3.2609081200.0123456789abcdef0123456789abcdef01234567",
        "bin": "2609081200_EX-1_ExampleDeviceFw_V1-2-3_0123456.bin",
        "hash7": "0123456",
    }
    assert parse_banner(b"") == {}
    assert parse_banner(b"[INF] - FW Name: X\r\n") == {}


def test_ALX1544_P65_parse_line_splits_timestamp_level_text():
    line = parse_line(b"[2000-01-01 00:00:00.103] [INF] AlxParamGroup_CrcOkSame_UsedCopyA\r\n")
    assert line == TraceLine("2000-01-01 00:00:00.103", "INF", "AlxParamGroup_CrcOkSame_UsedCopyA")
    assert parse_line("[2000-01-01 00:00:01.000] [ERR] Settings memory failed") == TraceLine(
        "2000-01-01 00:00:01.000", "ERR", "Settings memory failed"
    )
    assert parse_line(b"[2000-01-01 00:00:00.100] [INF] ") == TraceLine(
        "2000-01-01 00:00:00.100", "INF", ""
    )
    assert parse_line(b'{"status":"success"}\r\n') is None
    assert parse_line(b"plain text without a header") is None
    assert parse_line(b"") is None


def test_ALX1544_P66_parse_lines_keeps_only_trace_lines():
    raw = (
        BANNER
        + b'{"status":"success"}\r\n\xff\xfe noise\r\n[2000-01-01 00:00:00.200] [WRN] late\r\n'
    )
    lines = parse_lines(raw)
    assert [ln.level for ln in lines] == ["INF"] * 6 + ["WRN"]
    assert lines[0].text == "APP START"
    assert lines[-1] == TraceLine("2000-01-01 00:00:00.200", "WRN", "late")
    assert parse_lines(b"") == []


@given(
    ts=st.text(st.characters(codec="ascii", exclude_characters="]\r\n"), max_size=30),
    level=st.sampled_from(["INF", "WRN", "ERR", "DBG"]),
    text=st.text(st.characters(codec="ascii", exclude_characters="\r\n"), max_size=60),
    crlf=st.booleans(),
)
def test_ALX1544_P67_property_a_formatted_trace_line_parses_back_to_its_fields(
    ts, level, text, crlf
):
    line = f"[{ts}] [{level}] {text}".encode("ascii") + (b"\r\n" if crlf else b"\n")
    parsed = parse_line(line)
    assert parsed is not None
    assert (parsed.timestamp, parsed.level, parsed.text) == (ts, level, text.rstrip("\r"))
    assert parse_lines(line * 3) == [parsed] * 3
