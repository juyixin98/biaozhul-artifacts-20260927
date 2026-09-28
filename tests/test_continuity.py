"""Tests for per-PID continuity counter semantics."""
from __future__ import annotations

from app.core.packets import TS_PACKET_SIZE
from tests.conftest import analyze, event_codes, events_of
from tests.fixtures.ts_builder import (
    AUDIO_PID,
    CCGen,
    PMT_PID,
    VIDEO_PID,
    pat_section,
    pmt_section,
    packetize_section,
    scenario_clean,
    ts_packet,
)


def _base_stream():
    cc = CCGen()
    packets = []
    packets += packetize_section(0, pat_section({1: PMT_PID}), cc)
    packets += packetize_section(
        PMT_PID, pmt_section(1, [(0x1B, VIDEO_PID), (0x0F, AUDIO_PID)]), cc
    )
    return packets, cc


def test_clean_stream_has_no_continuity_errors():
    scenario = scenario_clean()
    result = analyze(scenario.data)
    assert "continuity_lost" not in event_codes(result)
    assert "duplicate_packet" not in event_codes(result)
    video = result.continuity.state_for(VIDEO_PID)
    assert video.duplicates == 0
    assert video.lost == 0
    assert video.stalls == 0


def test_duplicate_packet_detected_and_ignored_by_reassembly():
    result = analyze(__import__(
        "tests.fixtures.ts_builder", fromlist=["SCENARIOS"]
    ).SCENARIOS["duplicate_packet"]().data)

    dup = events_of(result, "duplicate_packet")
    assert len(dup) == 1
    assert dup[0].pid == VIDEO_PID
    assert dup[0].context["continuity_counter"] >= 0
    video = result.continuity.state_for(VIDEO_PID)
    assert video.duplicates == 1
    assert video.lost == 0

    # Duplicate bytes must not be appended a second time: exactly one PES
    # record exists, structurally complete, payload matches the builder.
    records = [r for r in result.pes.records if r.pid == VIDEO_PID]
    assert len(records) == 1
    assert records[0].complete is True
    assert records[0].gap is False
    assert records[0].payload_bytes == 200


def test_missing_packets_count_gap_and_mark_pes():
    scenario = __import__(
        "tests.fixtures.ts_builder", fromlist=["SCENARIOS"]
    ).SCENARIOS["missing_packets"]()
    result = analyze(scenario.data)

    lost = events_of(result, "continuity_lost")
    assert len(lost) == 1
    assert lost[0].pid == VIDEO_PID
    assert lost[0].context["missing"] == 2

    video = result.continuity.state_for(VIDEO_PID)
    assert video.lost == 2

    records = [r for r in result.pes.records if r.pid == VIDEO_PID]
    assert len(records) == 1
    assert records[0].gap is True
    assert records[0].complete is False
    assert "pes_gap" in event_codes(result)


def test_adaptation_only_packets_and_declared_discontinuity():
    scenario = __import__(
        "tests.fixtures.ts_builder", fromlist=["SCENARIOS"]
    ).SCENARIOS["adaptation_fields"]()
    result = analyze(scenario.data)
    codes = event_codes(result)

    assert "af_only_cc_increment" in codes
    af_info = events_of(result, "af_only_cc_increment")
    assert len(af_info) == 1
    assert af_info[0].pid == VIDEO_PID

    disc = events_of(result, "discontinuity_indicator")
    assert len(disc) == 1
    assert disc[0].pid == VIDEO_PID

    # Declared discontinuity never books real losses.
    video = result.continuity.state_for(VIDEO_PID)
    assert video.declared_discontinuities == 1
    assert video.lost == 0
    assert "continuity_lost" not in codes
    assert "duplicate_packet" not in codes


def test_cc_stall_with_different_payload_is_error_not_duplicate():
    packets, cc = _base_stream()
    # Full 184-byte payloads keep the comparison unambiguous (no stuffing
    # bytes involved). These packets are not PES starts; continuity is the
    # layer under test here.
    first = ts_packet(VIDEO_PID, b"\x01" * 184, cc=cc.next(VIDEO_PID), pusi=True)
    first_cc = first[3] & 0x0F
    stalled = ts_packet(VIDEO_PID, b"\x02" * 184, cc=first_cc, pusi=False)
    cont = ts_packet(
        VIDEO_PID, b"\x03" * 184, cc=(first_cc + 1) & 0xF, pusi=False
    )
    cc.set(VIDEO_PID, (first_cc + 1) & 0xF)
    packets += [first, stalled, cont]
    result = analyze(b"".join(packets))

    stall = events_of(result, "cc_stall_without_duplicate")
    assert len(stall) == 1
    assert stall[0].pid == VIDEO_PID
    # Context redacts payload bytes to length summaries.
    assert stall[0].context["payload_bytes"] == "<bytes:184>"
    assert "duplicate_packet" not in event_codes(result)
    assert result.continuity.state_for(VIDEO_PID).stalls == 1


def test_adaptation_only_repeating_cc_is_silent():
    packets, cc = _base_stream()
    payload_cc = cc.next(VIDEO_PID)
    p1 = ts_packet(VIDEO_PID, b"\x01" * 30, cc=payload_cc, pusi=True,
                   force_afc=3)
    # adaptation-only reusing exact CC: legal, must be silent
    af1 = ts_packet(VIDEO_PID, b"", cc=payload_cc, force_afc=2)
    af2 = ts_packet(VIDEO_PID, b"", cc=payload_cc, force_afc=2)
    next_cc = (payload_cc + 1) & 0xF
    p2 = ts_packet(VIDEO_PID, b"\x02" * 30, cc=next_cc, force_afc=3)
    cc.set(VIDEO_PID, next_cc)
    packets += [p1, af1, af2, p2]
    result = analyze(b"".join(packets))
    codes = event_codes(result)
    assert "continuity_lost" not in codes
    assert "af_only_cc_increment" not in codes
    assert "duplicate_packet" not in codes


def test_pid_counters_are_independent():
    packets, cc = _base_stream()
    # Drop a packet on video only; audio must stay clean.
    vp = [
        ts_packet(VIDEO_PID, b"\xAA" * 20, cc=cc.next(VIDEO_PID), pusi=True),
        ts_packet(VIDEO_PID, b"\xBB" * 20, cc=cc.next(VIDEO_PID)),
    ]
    cc.next(VIDEO_PID)  # consume a CC value without emitting its packet
    vp.append(ts_packet(VIDEO_PID, b"\xCC" * 20, cc=cc.next(VIDEO_PID)))
    ap = [
        ts_packet(AUDIO_PID, b"\x11" * 20, cc=cc.next(AUDIO_PID), pusi=True),
        ts_packet(AUDIO_PID, b"\x22" * 20, cc=cc.next(AUDIO_PID)),
        ts_packet(AUDIO_PID, b"\x33" * 20, cc=cc.next(AUDIO_PID)),
    ]
    result = analyze(b"".join(packets + vp + ap))
    lost = events_of(result, "continuity_lost")
    assert len(lost) == 1
    assert lost[0].pid == VIDEO_PID
    assert result.continuity.state_for(AUDIO_PID).lost == 0
    assert result.continuity.state_for(VIDEO_PID).lost == 1
