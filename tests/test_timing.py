"""Timing / signal kernel tests (PCR statistics, PTS/DTS collection)."""
from __future__ import annotations

from app.core.analyzer import analyze
from tools.tsbuilder import (
    pat_section,
    pes_packet,
    pes_packets,
    pmt_section,
    section_packets,
    ts_packet,
)

PMT_PID = 0x1000
VIDEO_PID = 0x1100
PROGRAM = 0x300


def _stream_with_pcrs(pcrs: list[int], *, discontinuity_at: int | None = None):
    parts = []
    pat = pat_section({PROGRAM: PMT_PID}, version=1)
    parts += section_packets(0, pat, start_cc=0)
    pmt = pmt_section(PROGRAM, PMT_PID, [(0x1B, VIDEO_PID)],
                      version=1, pcr_pid=VIDEO_PID)
    parts += section_packets(PMT_PID, pmt, start_cc=0)
    cc = 0
    for i, pcr in enumerate(pcrs):
        di = discontinuity_at is not None and i == discontinuity_at
        parts.append(ts_packet(VIDEO_PID, b"\x00" * 100, pusi=False,
                               cc=cc % 16, pcr=pcr, discontinuity=di))
        cc += 1
    return b"".join(parts)


def test_pcr_monotonic_stream_has_no_findings():
    pcrs = [i * 27_000_000 for i in range(1, 6)]  # 1s apart @27MHz
    data = _stream_with_pcrs(pcrs)
    r = analyze(data, "t-pcr-ok")
    assert not [f for f in r.findings if f.code == "pcr_non_monotonic"]
    t = r.timing[VIDEO_PID]
    assert t.pcr_samples == 5
    assert t.pcr_min_interval_27mhz == 27_000_000
    assert t.pcr_max_interval_27mhz == 27_000_000


def test_pcr_going_backwards_without_di_is_flagged():
    pcrs = [27_000_000, 2 * 27_000_000, 27_000_000 + 5]  # third < second
    data = _stream_with_pcrs(pcrs)
    r = analyze(data, "t-pcr-back")
    bad = [f for f in r.findings if f.code == "pcr_non_monotonic"]
    assert len(bad) == 1
    assert bad[0].details["pcr"] < bad[0].details["previous_pcr"]
    assert bad[0].pid == VIDEO_PID


def test_pcr_jump_with_discontinuity_indicator_is_not_an_error():
    pcrs = [27_000_000, 5 * 27_000_000]
    data = _stream_with_pcrs(pcrs, discontinuity_at=1)
    r = analyze(data, "t-pcr-di")
    assert not [f for f in r.findings if f.code == "pcr_non_monotonic"]


def test_pts_dts_are_counted_per_pid():
    parts = []
    pat = pat_section({PROGRAM: PMT_PID}, version=1)
    parts += section_packets(0, pat, start_cc=0)
    pmt = pmt_section(PROGRAM, PMT_PID, [(0x1B, VIDEO_PID)],
                      version=1, pcr_pid=VIDEO_PID)
    parts += section_packets(PMT_PID, pmt, start_cc=0)
    pes = pes_packet(0xE0, b"\xAB" * 200, pts=9000, dts=7200)
    parts += pes_packets(VIDEO_PID, pes, start_cc=0)
    r = analyze(b"".join(parts), "t-pts")
    t = r.timing[VIDEO_PID]
    assert t.pts_samples == 1 and t.dts_samples == 1
