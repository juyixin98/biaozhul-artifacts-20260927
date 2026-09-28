"""Tests for byte-stream sync: locking, finite recovery, mid-stream loss, tails."""
from __future__ import annotations

import pytest

from app.core.packets import TS_PACKET_SIZE
from app.core.sync import confirm_sync, find_sync, sync_candidate_offsets
from tests.conftest import analyze, event_codes, events_of
from tests.fixtures.ts_builder import (
    PACKET_SIZE,
    PMT_PID,
    PROGRAM_NUMBER,
    SCENARIOS,
)


def test_clean_stream_locks_at_zero():
    scenario = SCENARIOS["clean"]()
    result = analyze(scenario.data, record_id="sync-clean")
    assert result.framing.fatal is None
    assert result.framing.packets_parsed == len(scenario.data) // PACKET_SIZE
    assert result.framing.bytes_skipped == 0
    locked = events_of(result, "sync_locked")
    assert len(locked) == 1
    assert locked[0].offset == 0
    assert locked[0].record_id == "sync-clean"


def test_sync_loss_prefix_middle_and_trailing():
    scenario = SCENARIOS["sync_loss"]()
    exp = scenario.expectation
    result = analyze(scenario.data, record_id="sync-loss")

    assert result.framing.fatal is None
    recovered = events_of(result, "sync_recovered")
    assert len(recovered) == 2
    # Front garbage: exactly 55 bytes skipped, event states why.
    assert recovered[0].context["skipped_bytes"] == exp["garbage_prefix"]
    # Mid-stream garbage: exactly 301 bytes skipped.
    assert recovered[1].context["skipped_bytes"] == exp["garbage_middle"]
    assert result.framing.bytes_skipped == exp["garbage_prefix"] + exp["garbage_middle"]
    # Every originally valid packet is parsed despite the damage.
    assert result.framing.packets_parsed == exp["clean_packet_count"]
    # 7 trailing bytes are reported, never silently dropped or padded.
    assert result.framing.leftover_bytes == exp["trailing"]
    assert event_codes(result).count("truncated_tail") == 1
    assert event_codes(result).count("sync_lost") == 1


def test_sync_loss_reestablishes_cc_without_fabricating_losses():
    scenario = SCENARIOS["sync_loss"]()
    result = analyze(scenario.data)
    # No counter loss may be reported for the packet sequence that sits
    # behind an unparseable garbage span; a reset event takes its place.
    assert "cc_reset_after_sync_loss" in event_codes(result)
    cc_snapshot = result.continuity.snapshot()
    for pid, state in cc_snapshot.items():
        assert state.lost == 0, f"pid {pid:#x}: {state.lost} phantom losses"


def test_program_map_still_parsed_after_garbage():
    scenario = SCENARIOS["sync_loss"]()
    result = analyze(scenario.data)
    pat = result.programs.snapshot()["pat"]
    assert pat is not None
    assert {p["program_number"]: p["pid"] for p in pat["programs"]} == {
        PROGRAM_NUMBER: PMT_PID
    }
    pes_records = result.pes.records
    assert len(pes_records) == scenario.expectation["pes_count"]


def test_finite_scan_fails_within_bounded_window():
    # Garbage with 0x47-like noise but no confirmed 188-period structure.
    garbage = bytes((i * 7 + 3) % 256 for i in range(600)).replace(b"G", b"\x00")
    # make sure there are no sync bytes at all in the first 500 bytes
    garbage = bytes(b if b != 0x47 else 0x00 for b in garbage)
    result = analyze(garbage, max_sync_scan_bytes=400)
    assert result.framing.fatal is not None
    fatal_events = events_of(result, "sync_recovery_failed")
    assert len(fatal_events) == 1
    assert fatal_events[0].context["max_scan_bytes"] == 400
    assert fatal_events[0].context["scanned_bytes"] == 400
    assert result.framing.packets_parsed == 0


def test_false_sync_candidates_are_rejected_by_confirmation():
    scenario = SCENARIOS["clean"]()
    packets = [scenario.data[i:i + PACKET_SIZE]
               for i in range(0, len(scenario.data), PACKET_SIZE)]
    # Insert one 0x47 byte at offset 13 (not a packet boundary); the very
    # first packet remains intact so initial lock succeeds, proving the
    # candidate scan itself rejects decoys.
    decoy = bytearray(packets[0])
    decoy[13] = 0x47
    packets[0] = bytes(decoy)
    result = analyze(b"".join(packets))
    assert result.framing.fatal is None
    assert result.framing.packets_parsed == len(packets)


def test_single_sync_byte_then_garbage_is_indeterminate():
    # One 0x47 followed by bytes guaranteed not to contain the sync byte and
    # not to show the 188-period confirmation pattern.
    tail = bytes((i * 31 + 7) & 0xFF for i in range(400)).replace(b"\x47", b"\x00")
    data = b"\x47" + tail
    assert data.count(0x47) == 1
    result = analyze(data, sync_confirm_packets=3, max_sync_scan_bytes=400)
    assert result.framing.fatal is not None
    assert result.framing.packets_parsed == 0


def test_sync_helpers():
    # Unit-level checks for the numpy scan and confirmation.
    data = bytearray(b"\x00" * 10 + b"\x47" + b"\x00" * 500)
    data[10 + 188] = 0x47
    data[10 + 376] = 0x47
    found = find_sync(bytes(data), start=0, max_scan_bytes=64)
    assert found.offset == 10
    assert found.skipped_bytes == 10
    assert confirm_sync(bytes(data), 10, confirm_packets=3) is True
    assert confirm_sync(bytes(data), 9, confirm_packets=1) is False
    candidates = sync_candidate_offsets(bytes(data), 0, len(data))
    assert 10 in set(candidates.tolist())
    assert 10 + 188 in set(candidates.tolist())


def test_truncated_last_packet_reported_not_parsed():
    scenario = SCENARIOS["clean"]()
    data = scenario.data + b"\x47\x01\x02\x03"
    result = analyze(data)
    assert result.framing.leftover_bytes == 4
    assert result.framing.packets_parsed == len(scenario.data) // PACKET_SIZE
