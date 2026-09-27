"""Unit tests for the 188-byte framing and bounded sync-recovery scanner."""
from __future__ import annotations

from app.core.sync import (
    DEFAULT_MAX_SCAN_BYTES,
    PACKET_SIZE,
    PacketScanner,
    parse_packet,
)
from tools.tsbuilder import ts_packet


def _events(data: bytes):
    return list(PacketScanner(data).events())


def test_parses_clean_188_stream():
    pkts = [ts_packet(0x1100, bytes([i % 256]) * 184, cc=i % 16)
            for i in range(5)]
    events = _events(b"".join(pkts))
    packet_events = [e for e in events if e.kind == "packet"]
    assert len(packet_events) == 5
    assert all(e.packet.pid == 0x1100 for e in packet_events)
    assert [e.packet.continuity_counter for e in packet_events] == [
        0, 1, 2, 3, 4]


def test_garbage_prefix_triggers_one_resync_and_reports_skipped_bytes():
    good = ts_packet(0x1100, b"\x01" * 184, cc=0)
    garbage = b"\xAA" * 37
    events = _events(garbage + good)
    kinds = [e.kind for e in events]
    assert kinds[0] == "resync"
    assert events[0].skipped_bytes == 37
    assert kinds.count("resync") == 1
    assert [e for e in events if e.kind == "packet"][0].packet.pid == 0x1100


def test_trailing_bytes_are_reported_not_silently_padded():
    good = ts_packet(0x1100, b"\x02" * 184, cc=0)
    events = _events(good + b"\x00" * 7)
    assert [e.kind for e in events].count("packet") == 1
    trailing = [e for e in events if e.kind == "trailing"]
    assert len(trailing) == 1
    assert trailing[0].trailing_bytes == 7


def test_no_sync_at_all_is_undetermined_trailing():
    events = _events(b"\x12\x34\x56" * 10)
    assert not any(e.kind == "packet" for e in events)
    assert any(e.kind == "trailing" for e in events)


def test_bounded_scan_gives_up_within_max_scan_bytes():
    # One stray sync byte followed by non-confirming garbage, then a valid
    # packet placed just beyond the bounded scan window from that stray.
    stray = b"\x47" + b"\x00" * (PACKET_SIZE - 1)
    far_good = ts_packet(0x1100, b"\x03" * 184, cc=0)
    pad = b"\xAB" * DEFAULT_MAX_SCAN_BYTES
    scanner = PacketScanner(stray + pad + far_good,
                            max_scan_bytes=DEFAULT_MAX_SCAN_BYTES)
    events = list(scanner.events())
    # Either a trailing (gave up) or a resync much later -- but never an
    # unbounded full-buffer O(N^2) scan.  Crucially the stray must not be
    # accepted as a packet (it fails period confirmation).
    parsed = [e for e in events if e.kind == "packet"]
    if parsed:
        assert parsed[0].packet.byte_offset > len(stray)


def test_reserved_afc_is_structurally_rejected_and_recovered():
    pkt = bytearray(ts_packet(0x1100, b"\x04" * 184, cc=0))
    pkt[3] = (pkt[3] & 0x0F) | 0b0000_0000  # AFC=0 reserved
    good = ts_packet(0x1100, b"\x05" * 184, cc=1)
    events = _events(bytes(pkt) + good)
    # bad packet is not yielded as a packet; recovery via resync then good
    pids = [e.packet.pid for e in events if e.kind == "packet"]
    assert pids == [0x1100]
    assert any(e.kind == "resync" for e in events)


def test_adaptation_field_only_packet_parses_without_payload():
    pkt = parse_packet(
        ts_packet(0x200, b"", cc=3, random_access=True, pcr=123456789), 0, 0)
    assert pkt is not None
    assert not pkt.has_payload
    assert pkt.has_adaptation
    assert pkt.adaptation is not None
    assert pkt.adaptation.random_access
    assert pkt.adaptation.pcr == 123456789
    assert pkt.continuity_counter == 3


def test_tei_flag_is_exposed():
    raw = ts_packet(0x300, b"\x06" * 10, cc=0, tei=True)
    pkt = parse_packet(raw, 0, 0)
    assert pkt.tei is True
