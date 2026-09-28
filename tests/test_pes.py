"""Tests for restricted PES reassembly: boundaries, gaps, PTS/DTS, caps."""
from __future__ import annotations

from app.core.pes import parse_pes_header
from tests.conftest import analyze, event_codes, events_of
from tests.fixtures import ts_builder as tb
from tests.fixtures.ts_builder import (
    AUDIO_PID,
    CCGen,
    PMT_PID,
    VIDEO_PID,
    packetize_pes,
    packetize_section,
    pat_section,
    pmt_section,
    pes_packet,
    ts_packet,
)


def _map_only(video_pid=VIDEO_PID, audio_pid=AUDIO_PID, streams=True):
    cc = CCGen()
    packets = []
    stream_list = []
    if streams:
        stream_list = [(0x1B, video_pid), (0x0F, audio_pid)]
    packets += packetize_section(0, pat_section({1: PMT_PID}), cc)
    packets += packetize_section(PMT_PID, pmt_section(1, stream_list,
                                                      pcr_pid=video_pid), cc)
    return packets, cc


def test_pes_split_across_packets_has_header_and_payload():
    result = analyze(tb.SCENARIOS["cross_packet_sections"]().data)
    records = [r for r in result.pes.records if r.pid == VIDEO_PID]
    assert len(records) == 1
    rec = records[0]
    assert rec.complete is True
    assert rec.gap is False
    assert rec.header.stream_id == 0xE0
    assert rec.header.pts == 45_000
    assert rec.header.dts is None
    assert rec.payload_bytes == 250
    assert rec.close_reason == "end_of_input"


def test_pes_pts_and_dts_parse():
    packets, cc = _map_only()
    pes = pes_packet(0xE0, bytes(range(120)), pts=90_000 * 10,
                     dts=90_000 * 10 - 3000)
    packets += packetize_pes(VIDEO_PID, pes, cc)
    result = analyze(b"".join(packets))
    rec = [r for r in result.pes.records if r.pid == VIDEO_PID][0]
    assert rec.header.pts == 900_000
    assert rec.header.dts == 897_000
    assert rec.payload_bytes == 120
    assert rec.complete is True


def test_missing_middle_packets_gap_is_distinct_from_clean_boundary():
    result = analyze(tb.SCENARIOS["missing_packets"]().data)
    records = [r for r in result.pes.records if r.pid == VIDEO_PID]
    assert len(records) == 1
    rec = records[0]
    assert rec.gap is True
    assert rec.complete is False
    assert rec.header.pts == 180_000
    # Packet 0 contributes 170 ES bytes (184 payload - 14 PES header), the
    # surviving short final packet contributes 183; the 368 bytes of the two
    # dropped full interior packets never arrive.
    assert rec.payload_bytes == 170 + 183
    assert rec.payload_bytes < 700


def test_unbounded_video_pes_closed_by_next_pusi():
    packets, cc = _map_only(audio_pid=0x103)
    # A PES_packet_length == 0 unit has no declared end, so it must end on a
    # full packet boundary (next PUSI) for exact byte accounting:
    # 14-byte PTS PES header + 354 ES bytes == 368 == 2 * 184.
    payload1 = bytes((i * 3) & 0xFF for i in range(354))
    payload2 = bytes((i * 5 + 1) & 0xFF for i in range(100))
    pes1 = pes_packet(0xE0, payload1, pts=90_000, pes_length=0)
    pes2 = pes_packet(0xE0, payload2, pts=180_000, pes_length=0)
    assert len(pes1) == 368
    packets += packetize_pes(VIDEO_PID, pes1, cc)
    packets += packetize_pes(VIDEO_PID, pes2, cc)
    result = analyze(b"".join(packets))
    records = [r for r in result.pes.records if r.pid == VIDEO_PID]
    assert len(records) == 2
    first, second = records
    assert first.header.pes_packet_length == 0
    assert first.complete is True
    assert first.close_reason == "next_pusi"
    assert first.payload_bytes == 354
    # The second length-0 unit's last packet uses adaptation stuffing which
    # an unbounded PES cannot distinguish; its record is still delimited by
    # end of input and capped=false, but payload_bytes is an upper bound.
    assert second.payload_bytes >= 100
    assert second.complete is True


def test_incomplete_pes_at_end_of_input():
    packets, cc = _map_only()
    # A PES whose declared length says there are far more bytes than present.
    pes = pes_packet(0xE0, bytes(range(100)), pts=90_000, pes_length=500)
    packets += packetize_pes(VIDEO_PID, pes, cc)
    result = analyze(b"".join(packets))
    rec = [r for r in result.pes.records if r.pid == VIDEO_PID][0]
    assert rec.complete is False
    assert rec.close_reason == "end_of_input"
    assert "pes_incomplete" in event_codes(result)


def test_bad_start_code_is_dropped_with_reason():
    packets, cc = _map_only()
    bad = b"\x01\x02\x03" + b"\xEE" * 97
    packets.append(ts_packet(VIDEO_PID, bad, cc=cc.next(VIDEO_PID), pusi=True))
    packets.append(ts_packet(VIDEO_PID, b"\xAA" * 100, cc=cc.next(VIDEO_PID)))
    result = analyze(b"".join(packets))
    rec = [r for r in result.pes.records if r.pid == VIDEO_PID][0]
    assert rec.dropped is True
    assert rec.complete is False
    bad_events = events_of(result, "pes_bad_start_code")
    assert len(bad_events) == 1
    assert bad_events[0].context["first_bytes"] == "<bytes:3>"


def test_payload_cap_is_enforced_and_reported():
    packets, cc = _map_only()
    pes = pes_packet(0xE0, b"\x5A" * 4000, pts=90_000)
    packets += packetize_pes(VIDEO_PID, pes, cc)
    result = analyze(b"".join(packets), max_pes_payload_bytes=1024)
    rec = [r for r in result.pes.records if r.pid == VIDEO_PID][0]
    assert rec.capped is True
    assert rec.complete is False
    cap_events = events_of(result, "pes_payload_capped")
    assert len(cap_events) == 1
    assert cap_events[0].context["cap_bytes"] == 1024


def test_header_parser_unit():
    pes = pes_packet(0xC0, b"hello", pts=12345)
    header = parse_pes_header(pes)
    assert header is not None
    assert header.stream_id == 0xC0
    assert header.pts == 12345
    assert header.dts is None
    # PES_packet_length covers the 3 header_data/flag bytes, 5 PTS bytes and
    # the 5 payload bytes.
    assert header.pes_packet_length == 3 + header.header_data_length + 5

    # padding stream: no optional header
    padding = pes_packet(0xBE, b"\xFF" * 10)
    header = parse_pes_header(padding)
    assert header.header_data_length == 0
    assert header.pts is None
    assert header.pes_packet_length == 10

    # incomplete header bytes
    assert parse_pes_header(pes[:4]) is None
    assert parse_pes_header(b"\x00\x00\x00\xE0\x00\x00") is None
