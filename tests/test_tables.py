"""Tests for PAT/PMT parsing, cross-packet sections, CRC and atomic versions."""
from __future__ import annotations

from app.core.psi import parse_section, parse_pat, parse_pmt
from tests.conftest import analyze, event_codes, events_of
from tests.fixtures.ts_builder import (
    AUDIO_PID,
    AUDIO_TYPE,
    PMT_PID,
    PMT_PID_V2,
    PROGRAM_NUMBER,
    SCENARIOS,
    VIDEO_PID,
    VIDEO_TYPE,
    mpeg_crc32_bitwise,
    pat_section,
    pmt_section,
    packetize_section,
)
from tests.fixtures import ts_builder as tb


def test_clean_program_map_matches_fixture_literals():
    result = analyze(SCENARIOS["clean"]().data)
    snap = result.programs.snapshot()
    assert snap["pat"]["version"] == 0
    assert snap["pat"]["transport_stream_id"] == tb.TSID
    assert {p["program_number"]: p["pid"] for p in snap["pat"]["programs"]} == {
        PROGRAM_NUMBER: PMT_PID
    }
    pmt = snap["pmts"][0]
    assert pmt["pid"] == PMT_PID
    assert pmt["version"] == 0
    assert pmt["pcr_pid"] == VIDEO_PID
    assert {s["pid"]: s["stream_type"] for s in pmt["streams"]} == {
        VIDEO_PID: VIDEO_TYPE,
        AUDIO_PID: AUDIO_TYPE,
    }


def test_section_split_across_many_packets():
    result = analyze(SCENARIOS["cross_packet_sections"]().data)
    assert event_codes(result).count("table_crc_error") == 0
    assert event_codes(result).count("section_incomplete") == 0
    snap = result.programs.snapshot()
    assert {p["program_number"]: p["pid"] for p in snap["pat"]["programs"]} == {
        PROGRAM_NUMBER: PMT_PID
    }
    pmt = snap["pmts"][0]
    stream_map = {s["pid"]: s["stream_type"] for s in pmt["streams"]}
    # The cross-packet PMT also carries 38 filler streams for size; the
    # important assertion is that the video/audio entries survive the
    # continuation correctly and the CRC verifies.
    assert stream_map[VIDEO_PID] == VIDEO_TYPE
    assert stream_map[AUDIO_PID] == AUDIO_TYPE
    assert len(pmt["streams"]) == 40


def test_pointer_field_within_single_packet():
    """Two consecutive sections on one PID share a packet via pointer_field.

    A large PMT (40 streams, spans 3 packets) is immediately retransmitted:
    the tail of section #1 and the head of section #2 land in the same PUSI
    packet. Payload regions carry section bytes only; all short packets use
    adaptation-field stuffing (afc=3).
    """
    cc = tb.CCGen()
    streams = [(0x1B, 0x200 + i) for i in range(80)]
    section = pmt_section(PROGRAM_NUMBER, streams, pcr_pid=0x200)
    assert len(section) > 366  # guarantees a 3-packet span

    packets = tb.packetize_section(tb.PAT_PID,
                                   pat_section({PROGRAM_NUMBER: PMT_PID}), cc)
    # The PAT fits in one packet; pad to the 3-sync-byte confirmation window.
    pad_cc = tb.CCGen()
    packets.append(tb.ts_packet(0x1FFF, b"\xFF" * 184, cc=pad_cc.next(0x1FFF)))
    packets.append(tb.ts_packet(0x1FFF, b"\xFF" * 184, cc=pad_cc.next(0x1FFF)))

    s = section  # 416 bytes for 80 streams
    assert len(s) == 416
    # Section #1 occupies two full packets plus a 49-byte tail.
    packets.append(tb.ts_packet(PMT_PID, b"\x00" + s[:183],
                                cc=cc.next(PMT_PID), pusi=True))
    packets.append(tb.ts_packet(PMT_PID, s[183:367],
                                cc=cc.next(PMT_PID), pusi=False))
    tail1 = s[367:]
    assert len(tail1) == 49
    # Same PUSI packet: pointer_field -> 49-byte tail of #1, then the first
    # 134 bytes of #2, for a full 184-byte payload.
    packets.append(tb.ts_packet(
        PMT_PID, bytes([len(tail1)]) + tail1 + s[:134],
        cc=cc.next(PMT_PID), pusi=True))
    packets.append(tb.ts_packet(
        PMT_PID, s[134:318], cc=cc.next(PMT_PID), pusi=False))
    # Final short packet for section #2: adaptation-field stuffing.
    packets.append(tb.ts_packet(
        PMT_PID, s[318:], cc=cc.next(PMT_PID), pusi=False, force_afc=3))

    result = analyze(b"".join(packets))
    assert event_codes(result).count("table_crc_error") == 0
    switches = events_of(result, "pmt_version_switch")
    assert len(switches) == 1
    repeats = events_of(result, "pmt_version_repeat")
    assert len(repeats) == 1
    # The applied PMT really carries all 80 streams from the big section.
    pmt = result.programs.snapshot()["pmts"][0]
    assert len(pmt["streams"]) == 80
    assert pmt["streams"][0] == {"pid": 0x200, "stream_type": 0x1B}


def test_crc_error_rejects_section_and_keeps_map_empty():
    scenario = SCENARIOS["crc_error"]()
    result = analyze(scenario.data)
    crc_errors = events_of(result, "table_crc_error")
    assert len(crc_errors) == 1
    assert crc_errors[0].pid == 0
    assert crc_errors[0].context["table_id"] == 0x00
    assert crc_errors[0].context["stored_crc"] != crc_errors[0].context["computed_crc"]
    # Bad section must never become the current program map.
    assert result.programs.snapshot()["pat"] is None
    assert result.programs.pmt_pids() == set()
    assert result.programs.es_pids() == {}


def test_crc_implementations_agree():
    # Cross-check the independent oracle against the analyzer's implementation.
    from app.core.crc32 import mpeg_crc32

    sample = pat_section({PROGRAM_NUMBER: PMT_PID})
    assert mpeg_crc32(sample[:-4]) == mpeg_crc32_bitwise(sample[:-4])
    assert int.from_bytes(sample[-4:], "big") == mpeg_crc32_bitwise(sample[:-4])
    assert mpeg_crc32(b"") == mpeg_crc32_bitwise(b"") == 0xFFFFFFFF


def test_version_switch_is_atomic():
    result = analyze(SCENARIOS["version_switch"]().data)
    snap = result.programs.snapshot()

    # Final state: program now points at PMT v2 PID with v2 streams only.
    assert {p["program_number"]: p["pid"] for p in snap["pat"]["programs"]} == {
        PROGRAM_NUMBER: PMT_PID_V2
    }
    assert snap["pat"]["version"] == 1
    assert len(snap["pmts"]) == 1
    pmt = snap["pmts"][0]
    assert pmt["pid"] == PMT_PID_V2
    assert pmt["version"] == 1
    assert {s["pid"]: s["stream_type"] for s in pmt["streams"]} == {
        0x0121: VIDEO_TYPE,
        0x0122: AUDIO_TYPE,
    }
    assert pmt["pcr_pid"] == 0x0121

    switches = events_of(result, "pat_version_switch")
    assert len(switches) == 2
    v1 = switches[1]
    assert v1.context["old_version"] == 0
    assert v1.context["new_version"] == 1
    assert v1.context["old_programs"] == [[PROGRAM_NUMBER, PMT_PID]]
    assert v1.context["new_programs"] == [[PROGRAM_NUMBER, PMT_PID_V2]]
    assert v1.context["retired_pmt_pids"] == [PMT_PID]
    # Unchanged retransmission is recorded as repeat, never another switch.
    assert event_codes(result).count("pat_version_repeat") >= 1
    assert event_codes(result).count("pmt_version_switch") == 2


def test_not_current_sections_are_not_applied():
    result = analyze(SCENARIOS["not_current"]().data)
    assert result.programs.snapshot()["pat"] is None
    assert result.programs.pmt_pids() == set()
    assert result.programs.pmts == {}
    # The not-current PAT is always parsed (PID 0 is unconditionally PSI).
    # The not-current PMT is never routed to the table layer: with no current
    # PAT its PID is an unknown PID, which is the correct boundary semantics.
    not_current = events_of(result, "table_not_current")
    assert len(not_current) == 1
    assert not_current[0].pid == 0


def test_not_current_pmt_is_rejected_when_pat_is_current():
    cc = tb.CCGen()
    packets = tb.packetize_section(
        tb.PAT_PID, tb.pat_section({PROGRAM_NUMBER: PMT_PID}), cc
    )
    packets += tb.packetize_section(
        PMT_PID,
        tb.pmt_section(PROGRAM_NUMBER,
                       [(VIDEO_TYPE, VIDEO_PID), (AUDIO_TYPE, AUDIO_PID)],
                       current_next=False),
        cc,
    )
    null_cc = tb.CCGen()
    while len(packets) < 3:
        packets.append(tb.ts_packet(0x1FFF, b"\xFF" * 184,
                                    cc=null_cc.next(0x1FFF)))
    result = analyze(b"".join(packets))
    # PAT applies; the PMT is parsed (its PID is in the PAT) but rejected as
    # not-yet-current, so no streams exist.
    assert result.programs.snapshot()["pat"] is not None
    assert result.programs.es_pids() == {}
    not_current = events_of(result, "table_not_current")
    assert {e.pid for e in not_current} == {PMT_PID}


def test_section_parser_unit_fields():
    section = parse_section(pat_section({PROGRAM_NUMBER: PMT_PID}, version=3))
    assert section is not None
    assert section.table_id == 0x00
    assert section.version == 3
    assert section.current_next is True
    assert section.crc_ok is True
    parsed = parse_pat(section)
    assert parsed.programs[0].program_number == PROGRAM_NUMBER
    assert parsed.programs[0].pid == PMT_PID

    pmt = parse_section(pmt_section(
        PROGRAM_NUMBER, [(VIDEO_TYPE, VIDEO_PID), (AUDIO_TYPE, AUDIO_PID)],
        pcr_pid=VIDEO_PID,
    ))
    parsed_pmt = parse_pmt(pmt)
    assert parsed_pmt.pcr_pid == VIDEO_PID
    assert [(s.stream_type, s.pid) for s in parsed_pmt.streams] == [
        (VIDEO_TYPE, VIDEO_PID),
        (AUDIO_TYPE, AUDIO_PID),
    ]
