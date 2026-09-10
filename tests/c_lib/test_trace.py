# SPDX-License-Identifier: MIT
"""alx.c_lib.trace - parsers of the C library trace output (no device).

Proofs (ALX-1544):
  P60 parse_banner extracts name, version, bin and the 7-char build hash from a boot transcript; {} without one
  P65 parse_line splits one [timestamp] [LEVEL] text line and returns None for anything else
  P66 parse_lines keeps only the trace lines of a mixed transcript (banner, JSON frame, noise)
  P67 property: a formatted trace line parses back to its fields (Hypothesis)
  P121 mutation-driven hardening: TraceLine is immutable
  P122 mutation-driven hardening: undecodable bytes are replaced, never fatal; a bin name without an
       underscore yields its stem as hash7

Proofs (ALX-1553):
  P29 parse_id_trace reads the AlxId_Trace block: the four identity fields plus hash7
  P30 a device behind a bootloader emits the block twice - the LAST block wins and the bootloader
      section arrives under boot_ keys
  P31 inventory lines outside the identity sections are ignored, and a transcript without the block
      gives {}
"""

import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from alx.c_lib.trace import TraceLine, parse_banner, parse_id_trace, parse_line, parse_lines

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


def test_ALX1544_P121_trace_line_is_immutable():
    line = parse_line(b"[t] [INF] x")
    assert line is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        line.text = "y"  # type: ignore[misc]


def test_ALX1544_P122_undecodable_bytes_are_replaced_and_underscore_free_bin_keeps_its_stem():
    banner = BANNER.replace(b"FW Name: ExampleDeviceFw", b"FW Name: Example\xffDeviceFw")
    assert parse_banner(banner)["name"] == "Example\ufffdDeviceFw"
    assert parse_line(b"[t] [WRN] caf\xe9") == TraceLine("t", "WRN", "caf\ufffd")
    plain = BANNER.replace(b"2609081200_EX-1_ExampleDeviceFw_V1-2-3_0123456.bin", b"image.bin")
    assert parse_banner(plain)["hash7"] == "image"


def _id_block(artf, name, ver, binfile, boot=None, inventory=True):
    """One AlxId_Trace block as the firmware writes it, optionally with the inventory noise."""
    out = [
        "AlxId_Trace - START",
        "FW:",
        f"- artf: {artf}",
        f"- name: {name}",
        f"- ver: {ver}",
        f"- bin: {binfile}",
        "- job_name: VisualGDB Local",
        "- job_number: 0",
    ]
    if inventory:
        out += [
            "Compiler:",
            "- name: C, ver: 199901",
            "- name: GCC",
            "- ver: 10.3.1",
            "HW:",
            "- pcb_artf: EX-1-2-3",
            "- pcb_name: Example",
            "- mcu_name: EXAMPLE",
        ]
    if boot:
        out += ["FW - Bootloader:"] + [f"- {k}: {v}" for k, v in boot.items()]
    return b"".join(f"[2000-01-01 00:00:00.001] [INF] {line}\r\n".encode("ascii") for line in out)


APP_ID = (
    "EX-1-2-3",
    "ExampleFw",
    "1.2.3.2601020304." + "ab" * 20,
    "2601020304_EX-1-2-3_ExampleFw_V1-2-3_abcdef1.bin",
)
BOOT_ID = {
    "artf": "EX-1-2-4",
    "name": "ExampleFw_Boot",
    "ver": "0.1.0.2601010101." + "cd" * 20,
    "bin": "2601010101_EX-1-2-4_ExampleFw_Boot_V0-1-0_9876543.bin",
}


def test_ALX1553_P29_parse_id_trace_reads_the_identity_block():
    ident = parse_id_trace(_id_block(*APP_ID, inventory=False))
    assert ident == {
        "artf": "EX-1-2-3",
        "name": "ExampleFw",
        "ver": "1.2.3.2601020304." + "ab" * 20,
        "bin": "2601020304_EX-1-2-3_ExampleFw_V1-2-3_abcdef1.bin",
        "hash7": "abcdef1",
    }


def test_ALX1553_P30_the_last_block_wins_and_the_bootloader_arrives_under_boot_keys():
    # a device behind a bootloader: the bootloader prints its own block, then the application prints
    # one carrying both identities. Only the second may describe what is running.
    boot_first = _id_block(BOOT_ID["artf"], BOOT_ID["name"], BOOT_ID["ver"], BOOT_ID["bin"])
    ident = parse_id_trace(boot_first + _id_block(*APP_ID, boot=BOOT_ID))
    assert ident["name"] == "ExampleFw", "the application, not the bootloader that printed first"
    assert ident["hash7"] == "abcdef1"
    assert ident["boot_name"] == "ExampleFw_Boot"
    assert ident["boot_artf"] == "EX-1-2-4"
    assert ident["boot_hash7"] == "9876543"


def test_ALX1553_P31_inventory_is_ignored_and_no_block_gives_nothing():
    ident = parse_id_trace(_id_block(*APP_ID, inventory=True))
    assert ident["name"] == "ExampleFw", "'- name: GCC' in the compiler list must not overwrite it"
    assert "pcb_artf" not in ident
    assert "mcu_name" not in ident
    assert parse_id_trace(BANNER) == {}, "a product banner is not an AlxId_Trace block"
    assert parse_id_trace(b"") == {}
    assert parse_id_trace(b"[t] [INF] AlxId_Trace - START\r\n") == {}, "a block with no section"
