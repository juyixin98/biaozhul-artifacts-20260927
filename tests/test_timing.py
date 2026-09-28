"""Tests for the time/signal kernel: PCR cadence, jitter, backwards PCR."""
from __future__ import annotations

from app.core.timing import PCR_HZ, TimingKernel
from app.diagnostics import Code, DiagnosticsCollector
from tests.conftest import analyze, events_of
from tests.fixtures import ts_builder as tb
from tests.fixtures.ts_builder import (
    VIDEO_PID,
    CCGen,
    packetize_section,
    pat_section,
    pmt_section,
    ts_packet,
)


def test_pcr_cadence_and_rate_from_synthetic_stream():
    result = analyze(tb.SCENARIOS["pcr_timing"]().data)
    timing = result.timing.snapshot()
    assert VIDEO_PID in timing
    stats = timing[VIDEO_PID]
    assert stats["samples"] == 11
    assert abs(stats["mean_pcr_interval_ms"] - 10.0) < 0.01
    assert stats["max_pcr_interval_ms"] == 10.0
    assert stats["jitter_us"] == 0.0
    # 11 packets, first->last PCR span = 100 ms. The byte distance includes
    # the final packet's 188 bytes (11 * 188 bytes transported in 0.1 s).
    expected_mbps = round((11 * 188) * 8 / 0.1 / 1_000_000.0, 6)
    assert abs(stats["transport_rate_mbps"] - expected_mbps) < 1e-6


def test_jitter_detected_via_irregular_intervals():
    diag = DiagnosticsCollector("timing-unit")
    kernel = TimingKernel(diag)
    # 10 ms, 10 ms, 15 ms intervals at one packet each.
    base = 0
    for i, interval_ms in enumerate([0, 10, 20, 35]):
        pcr = base + interval_ms * PCR_HZ // 1000
        kernel.add_pcr(VIDEO_PID, pcr, offset=i * 188)
    stats = kernel.stats_for_pid(VIDEO_PID)
    # deltas: 10, 10, 15 -> mean 11.6667, max abs dev 3.3333 ms = 3333.3 us
    assert stats["samples"] == 4
    assert stats["jitter_us"] > 3000.0


def test_backwards_pcr_restarts_baseline():
    diag = DiagnosticsCollector("timing-unit")
    kernel = TimingKernel(diag)
    kernel.add_pcr(VIDEO_PID, 10 * PCR_HZ, offset=0)
    kernel.add_pcr(VIDEO_PID, 20 * PCR_HZ, offset=188)
    kernel.add_pcr(VIDEO_PID, 5 * PCR_HZ, offset=376)  # backwards
    backwards = [e for e in diag.events if e.code == Code.PCR_BACKWARDS.value]
    assert len(backwards) == 1
    assert backwards[0].pid == VIDEO_PID
    assert backwards[0].context["previous_pcr"] == 20 * PCR_HZ
    # Series was restarted: only the backwards sample remains, no stats yet.
    assert kernel.stats_for_pid(VIDEO_PID) is None
    kernel.add_pcr(VIDEO_PID, 6 * PCR_HZ, offset=564)
    stats = kernel.stats_for_pid(VIDEO_PID)
    assert stats["samples"] == 2


def test_pcr_on_packet_with_adaptation_and_payload():
    cc = CCGen()
    packets = []
    packets += packetize_section(0, pat_section({1: 0x100}), cc)
    packets += packetize_section(
        0x100, pmt_section(1, [(0x1B, VIDEO_PID)], pcr_pid=VIDEO_PID), cc
    )
    packets.append(
        ts_packet(VIDEO_PID, b"\xAA" * 50, cc=cc.next(VIDEO_PID), pusi=True,
                  pcr=(0, 0), force_afc=3)
    )
    packets.append(
        ts_packet(VIDEO_PID, b"\xBB" * 50, cc=cc.next(VIDEO_PID),
                  force_afc=3, pcr=(900, 0))  # 10 ms at the 90 kHz PCR base
    )
    result = analyze(b"".join(packets))
    stats = result.timing.snapshot()[VIDEO_PID]
    assert stats["samples"] == 2
    assert abs(stats["mean_pcr_interval_ms"] - 10.0) < 0.001
