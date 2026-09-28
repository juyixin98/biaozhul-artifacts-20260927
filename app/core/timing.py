"""Time and signal kernel: PCR tracking, spacing/jitter and bitrate.

PCRs are tracked per PID (normally only the PMT-declared PCR PID, but the
kernel itself is PID-agnostic). Values are 27 MHz ticks (``base*300 +
extension``). Statistics over a PID's PCR series are computed with NumPy:

* ``mean_pcr_interval_ms`` / ``max_pcr_interval_ms``
* ``jitter_us``: max absolute deviation of successive PCR deltas from
  their mean
* ``transport_rate_mbps``: byte distance between the first and last PCR
  sample divided by their PCR time delta (Transport Stream bitrate).

A backwards PCR is a signal error, not a "negative delta": it is reported
and restarts the series baseline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..diagnostics import Code, DiagnosticsCollector

PCR_HZ = 27_000_000


@dataclass(frozen=True)
class PCRSample:
    offset: int
    pcr: int


class TimingKernel:
    def __init__(self, diagnostics: DiagnosticsCollector):
        self._diag = diagnostics
        self._samples: dict[int, list[PCRSample]] = {}

    def add_pcr(self, pid: int, pcr: int, offset: int) -> None:
        series = self._samples.setdefault(pid, [])
        if series:
            previous = series[-1]
            delta = pcr - previous.pcr
            if delta < 0:
                self._diag.warning(
                    Code.PCR_BACKWARDS,
                    "PCR value moved backwards; series baseline restarted",
                    pid=pid,
                    offset=offset,
                    previous_pcr=previous.pcr,
                    pcr=pcr,
                )
                series.clear()
        series.append(PCRSample(offset=offset, pcr=pcr))

    def series(self, pid: int) -> list[PCRSample]:
        return list(self._samples.get(pid, ()))

    def stats_for_pid(self, pid: int, packet_size: int = 188) -> Optional[dict]:
        series = self._samples.get(pid)
        if not series or len(series) < 2:
            return None
        pcrs = np.array([s.pcr for s in series], dtype=np.int64)
        offsets = np.array([s.offset for s in series], dtype=np.int64)
        pcr_deltas = np.diff(pcrs).astype(np.float64)
        intervals_ms = pcr_deltas / PCR_HZ * 1000.0
        # Exclude the interval that crossed a backwards-PCR reset (series
        # was cleared there, so consecutive samples are always increasing).
        mean_ms = float(np.mean(intervals_ms))
        jitter_us = float(
            np.max(np.abs(intervals_ms - np.mean(intervals_ms))) * 1000.0
        )
        first, last = series[0], series[-1]
        pcr_span_s = (last.pcr - first.pcr) / PCR_HZ
        byte_distance = int(last.offset - first.offset + packet_size)
        rate_mbps = None
        if pcr_span_s > 0:
            rate_mbps = round(byte_distance * 8 / pcr_span_s / 1_000_000.0, 6)
        return {
            "samples": len(series),
            "first_offset": first.offset,
            "last_offset": last.offset,
            "mean_pcr_interval_ms": round(mean_ms, 6),
            "max_pcr_interval_ms": round(float(np.max(intervals_ms)), 6),
            "jitter_us": round(jitter_us, 3),
            "transport_rate_mbps": rate_mbps,
        }

    def snapshot(self) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for pid in self._samples:
            stats = self.stats_for_pid(pid)
            if stats is not None:
                out[pid] = stats
        return out
