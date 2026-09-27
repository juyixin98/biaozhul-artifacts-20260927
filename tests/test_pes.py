"""Constrained PES reassembly tests."""
from __future__ import annotations

from app.core.pes import PesAssembler, parse_pes_header
from tools.tsbuilder import pes_packet, ts_packet


def _asm(cap=256 * 1024):
    return PesAssembler(0x1100, 0x1B, 0x300, cap)


def test_complete_bounded_pes_self_closes_without_next_pusi():
    asm = _asm()
    pes = pes_packet(0xE0, b"\xAA" * 300, pts=9000)
    packets = []
    rest = pes
    cc = 0
    first = True
    while True:
        chunk = rest[:184]
        rest = rest[184:]
        last = not rest
        payload = chunk + (b"\xff" * (184 - len(chunk)) if last else b"")
        packets.append(ts_packet(0x1100, payload, pusi=first, cc=cc))
        cc += 1
        first = False
        if last:
            break
    for i, raw in enumerate(packets):
        pkt_pusi = raw[1] & 0x40 != 0
        asm.feed(raw[4:], pkt_pusi, False, i)
    assert len(asm.completed) == 1
    frag = asm.completed[0]
    assert not frag.corrupt
    assert frag.data == pes
    assert frag.header.stream_id == 0xE0
    assert frag.header.pts == 9000


def test_pts_dts_decode():
    pes = pes_packet(0xE0, b"\xBB" * 50, pts=9000, dts=7200)
    header, why = parse_pes_header(pes)
    assert why is None and header is not None
    assert header.pts == 9000 and header.dts == 7200


def test_missing_start_code_is_rejected():
    header, why = parse_pes_header(b"\x01\x02\x03garbage")
    assert header is None
    assert "start code" in why


def test_unsignaled_gap_corrupts_inflight_pes():
    asm = _asm()
    pes = pes_packet(0xE0, b"\xCC" * 300, pts=9000)
    asm.feed(pes[:184], True, False, 0)
    # 2 packets lost mid-PES
    asm.signal_loss(1, missing=2, signaled=False)
    asm.feed(pes[184:368], False, False, 3)
    # fragment self-closes at declared length and is reported corrupt
    assert len(asm.completed) == 1
    assert asm.completed[0].corrupt
    assert asm.gap_count == 1
    codes = [f.code for f in asm.findings]
    assert "pes_gap" in codes
    assert asm.findings[0].details["missing_packets"] == 2


def test_signaled_discontinuity_does_not_count_a_gap():
    asm = _asm()
    pes = pes_packet(0xE0, b"\xDD" * 300, pts=9000)
    asm.feed(pes[:184], True, False, 0)
    asm.signal_loss(1, missing=0, signaled=True)  # reset, not corruption
    # a fresh PES begins
    pes2 = pes_packet(0xE0, b"\xEE" * 100, pts=27000)
    asm.feed(pes2[:184], True, False, 2)
    assert asm.gap_count == 0
    assert len(asm.completed) == 1
    assert asm.completed[0].data == pes2
    assert not asm.completed[0].corrupt


def test_duplicate_payload_is_not_concatenated():
    asm = _asm()
    chunk = b"\x00\x00\x01\xE0\x00\x08" + b"\xAB" * 2  # 10-byte PES
    padded = chunk + b"\xff" * (184 - len(chunk))
    asm.feed(padded, True, False, 0)
    asm.feed(padded, False, True, 1)   # duplicate, must be ignored
    asm.flush_end()
    # Exactly one fragment of exactly 14? header says length 8 -> total 14.
    assert len(asm.completed) == 1
    assert len(asm.completed[0].data) == 14
    assert asm.completed[0].data == padded[:14]


def test_oversize_pes_is_rejected_at_cap():
    asm = _asm(cap=200)
    asm.feed(b"\x00\x00\x01\xE0\x00\x00" + b"\x01" * 178,
             True, False, 0)  # unbounded video PES, 184 bytes
    asm.feed(b"\x02" * 184, False, False, 1)  # crosses 200-byte cap
    assert any(f.code == "pes_oversize" for f in asm.findings)
    # The oversize fragment is finalized as corrupt.
    assert asm.completed and asm.completed[0].corrupt


def test_truncated_bounded_pes_at_end_is_undetermined_rejection():
    asm = _asm()
    pes = pes_packet(0xE0, b"\xCC" * 300, pts=9000)
    asm.feed(pes[:184], True, False, 0)
    asm.flush_end()
    frag = asm.completed[0]
    assert frag.corrupt
    assert "declared bytes arrived" in (frag.corrupt_reason or "")


def test_continuation_without_start_is_undetermined():
    asm = _asm()
    asm.feed(b"\x99" * 184, False, False, 0)
    codes = [f.code for f in asm.findings]
    assert "pes_malformed" in codes
    assert asm.findings[0].disposition.value == "undetermined"
