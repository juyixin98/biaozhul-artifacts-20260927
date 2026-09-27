"""PSI reassembly, CRC verification and atomic table-version tests."""
from __future__ import annotations

import pytest

from app.core.diagnostics import Disposition, Severity
from app.core.psi import (
    PAT_PID,
    SectionAssembler,
    TablesManager,
    crc32_mpeg2,
    parse_pat,
    parse_pmt,
)
from app.core.sync import parse_packet
from tools.tsbuilder import (
    corrupt_crc,
    pat_section,
    pmt_section,
    section_packets,
    ts_packet,
)


def _feed(asm, packets, pid=0):
    out = []
    for i, raw in enumerate(packets):
        pkt = parse_packet(raw, i, i * 188)
        out.extend(asm.feed(pkt.payload, pkt.pusi))
    return out


def test_crc32_mpeg2_known_vector():
    # CRC of the 3-byte ASCII "123" with the MPEG-2 polynomial is a stable
    # independently computed check value.
    assert f"{crc32_mpeg2(b'123'):08x}" == "d952f164"


def test_pat_section_roundtrip_and_parse():
    sec = pat_section({0x300: 0x1000, 0x400: 0x1200}, version=3)
    asm = SectionAssembler(PAT_PID, lambda: 0)
    raws = [x for x in _feed(
        asm, section_packets(PAT_PID, sec, start_cc=0)) if not hasattr(x, "severity")]
    assert len(raws) == 1
    pat, _, _ = parse_pat(raws[0].body)
    assert pat.programs == {0x300: 0x1000, 0x400: 0x1200}
    assert pat.version == 3


def test_cross_packet_section_is_reassembled():
    # Big PMT spanning 2 TS packets with capacity-aligned fragmentation.
    streams = [(0x1B, 0x200 + i) for i in range(60)]
    sec = pmt_section(0x300, 0x1000, streams, version=1, pcr_pid=0x200)
    assert len(sec) > 183  # genuinely spans packets
    asm = SectionAssembler(0x1000, lambda: 0)
    out = _feed(asm, section_packets(0x1000, sec, start_cc=0))
    raws = [x for x in out if not hasattr(x, "severity")]
    assert len(raws) == 1
    assert raws[0].body == sec
    prog, pcr_pid, parsed_streams = parse_pmt(raws[0].body)
    assert prog == 0x300 and pcr_pid == 0x200
    assert len(parsed_streams) == 60
    assert asm.flush_end() == []


def test_section_header_straddling_packet_boundary():
    # Only the first 2 bytes of the section fit in the PUSI packet.
    sec = pat_section({0x300: 0x1000}, version=1)
    p1 = ts_packet(0, b"\x00" + sec[:2] + b"\xff" * 181, pusi=True, cc=0)
    rest = sec[2:]
    p2 = ts_packet(0, rest[:184], cc=1)
    tail = rest[184:]
    p3 = ts_packet(0, tail + b"\xff" * (184 - len(tail)), cc=2)
    asm = SectionAssembler(0, lambda: 0)
    out = _feed(asm, [p1, p2, p3])
    raws = [x for x in out if not hasattr(x, "severity")]
    assert len(raws) == 1 and raws[0].body == sec


def test_crc_error_is_rejected_and_reports_both_crcs():
    sec = corrupt_crc(pmt_section(0x300, 0x1000, [(0x1B, 0x200)],
                                  version=1, pcr_pid=0x200))
    asm = SectionAssembler(0x1000, lambda: 4)
    out = _feed(asm, section_packets(0x1000, sec, start_cc=0))
    findings = [x for x in out if hasattr(x, "severity")]
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "section_crc_error"
    assert f.disposition == Disposition.REJECTED
    assert f.packet_index == 4
    assert "expected_crc" in f.details and "actual_crc" in f.details
    assert f.details["expected_crc"] != f.details["actual_crc"]


def test_truncated_section_at_end_is_undetermined_not_silently_ok():
    # A section that genuinely begins and continues but never completes.
    sec = pmt_section(0x300, 0x1000,
                      [(0x1B, 0x200 + i) for i in range(60)],
                      version=1, pcr_pid=0x200)
    assert len(sec) > 183
    packets = section_packets(0x1000, sec, start_cc=0)
    asm = SectionAssembler(0x1000, lambda: 0)
    # feed the first packet only; continuation bytes never arrive
    pkt = parse_packet(packets[0], 0, 0)
    asm.feed(pkt.payload, pkt.pusi)
    flushed = asm.flush_end()
    assert len(flushed) == 1
    assert flushed[0].code == "section_incomplete_at_end"
    assert flushed[0].disposition == Disposition.UNDETERMINED


def test_atomic_version_switch_replaces_map_once_complete():
    tm = TablesManager()
    # v1 complete -> committed
    pat1 = pat_section({0x300: 0x1000}, version=1)
    findings = []
    for i, raw in enumerate(section_packets(PAT_PID, pat1, start_cc=0)):
        pkt = parse_packet(raw, i, 0)
        findings += tm.feed_packet(PAT_PID, pkt.payload, pkt.pusi, i)
    assert tm.pat is not None and tm.pat.version == 1
    assert tm.pat.programs == {0x300: 0x1000}

    # A corrupted v2 must NOT mutate the live map.
    bad_pmt = corrupt_crc(pmt_section(
        0x300, 0x1000, [(0x1B, 0x200)], version=2, pcr_pid=0x200))
    idx = 1
    for raw in section_packets(0x1000, bad_pmt, start_cc=0):
        pkt = parse_packet(raw, idx, 0)
        f = tm.feed_packet(0x1000, pkt.payload, pkt.pusi, idx)
        findings += f
        idx += 1
    # No program info from the bad PMT may appear.
    assert 0x300 not in tm.programs or tm.programs[0x300].version != 2
    assert tm.elementary_pids() == {}
    assert any(x.code == "section_crc_error" for x in findings)


def test_pmt_version_switch_is_atomic_and_audited():
    tm = TablesManager()
    pat = pat_section({0x300: 0x1000}, version=1)
    for i, raw in enumerate(section_packets(PAT_PID, pat, start_cc=0)):
        pkt = parse_packet(raw, i, 0)
        tm.feed_packet(PAT_PID, pkt.payload, pkt.pusi, i)
    pmt1 = pmt_section(0x300, 0x1000, [(0x1B, 0x200), (0x0F, 0x201)],
                       version=1, pcr_pid=0x200)
    for i, raw in enumerate(section_packets(0x1000, pmt1, start_cc=0), 1):
        pkt = parse_packet(raw, i, 0)
        tm.feed_packet(0x1000, pkt.payload, pkt.pusi, i)
    assert set(tm.elementary_pids()) == {0x200, 0x201}

    pmt2 = pmt_section(0x300, 0x1000, [(0x1B, 0x200)],
                       version=2, pcr_pid=0x200)
    findings = []
    for i, raw in enumerate(section_packets(0x1000, pmt2, start_cc=0), 2):
        pkt = parse_packet(raw, i, 0)
        findings += tm.feed_packet(0x1000, pkt.payload, pkt.pusi, i)
    assert set(tm.elementary_pids()) == {0x200}  # 0x201 removed atomically
    switches = [f for f in findings if f.code == "table_version_switch"]
    assert len(switches) == 1
    assert switches[0].details["old_version"] == 1
    assert switches[0].details["new_version"] == 2


def test_pmt_pids_are_learned_from_pat_not_assumed():
    # Use a non-typical PMT PID (0x0150) and video PID (0x0160).
    tm = TablesManager()
    pat = pat_section({0x55: 0x0150}, version=1)
    for i, raw in enumerate(section_packets(PAT_PID, pat, start_cc=0)):
        pkt = parse_packet(raw, i, 0)
        tm.feed_packet(PAT_PID, pkt.payload, pkt.pusi, i)
    assert 0x0150 in tm.pmt_pids
    pmt = pmt_section(0x55, 0x0150, [(0x1B, 0x0160)],
                      version=1, pcr_pid=0x0160)
    for i, raw in enumerate(section_packets(0x0150, pmt, start_cc=0), 1):
        pkt = parse_packet(raw, i, 0)
        tm.feed_packet(0x0150, pkt.payload, pkt.pusi, i)
    assert tm.elementary_pids() == {0x0160: (0x55, 0x1B)}


def test_packet_loss_invalidates_inflight_section():
    sec = pmt_section(0x300, 0x1000,
                      [(0x1B, 0x200 + i) for i in range(60)],
                      version=1, pcr_pid=0x200)
    asm = SectionAssembler(0x1000, lambda: 9)
    packets = section_packets(0x1000, sec, start_cc=0)
    # feed only the first packet, then signal a gap -> no section completes
    pkt = parse_packet(packets[0], 0, 0)
    asm.feed(pkt.payload, pkt.pusi)
    f = asm.signal_gap()
    assert f is not None and f.code == "section_malformed"
    assert asm.flush_end() == []
