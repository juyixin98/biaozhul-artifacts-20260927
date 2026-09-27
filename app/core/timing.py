"""Timing & signal kernel: PCR / PTS-DTS statistics.

NumPy is used for the numerical reductions (interval stats, monotonicity).
PCR non-monotonicity is only flagged when no discontinuity was signaled at
that point -- a signaled discontinuity legitimately resets the clock.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .diagnostics import (
    PCR_NON_MONOTONIC,
    Disposition,
    Finding,
    Severity,
)

PCR_TICKS_PER_27MHZ = 300  # 27MHz base + 90kHz extension


@dataclass
class _PcrSample:
    packet_index: int
    byte_offset: int
    pcr: int
    signaled_discontinuity: bool


@dataclass
class PidTiming:
    pid: int
    pcr_samples: int = 0
    pcr_min_interval_27mhz: int | None = None
    pcr_mean_interval_27mhz: float | None = 0.0
    pcr_max_interval_27mhz: int | None = None
    pcr_non_monotonic: int = 0
    pts_samples: int = 0
    dts_samples: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "pcr_samples": self.pcr_samples,
            "pcr_min_interval_27mhz": self.pcr_min_interval_27mhz,
            "pcr_mean_interval_27mhz": self.pcr_mean_interval_27mhz,
            "pcr_max_interval_27mhz": self.pcr_max_interval_27mhz,
            "pcr_non_monotonic": self.pcr_non_monotonic,
            "pts_samples": self.pts_samples,
            "dts_samples": self.dts_samples,
        }


class TimingCollector:
    def __init__(self) -> None:
        self._pcr: dict[int, list[_PcrSample]] = {}
        self._pts_count: dict[int, int] = {}
        self._dts_count: dict[int, int] = {}

    def record_pcr(self, pid: int, packet_index: int, byte_offset: int,
                   pcr: int, signaled_discontinuity: bool) -> None:
        self._pcr.setdefault(pid, []).append(_PcrSample(
            packet_index, byte_offset, pcr, signaled_discontinuity))

    def record_pes_timing(self, pid: int, pts: int | None,
                          dts: int | None) -> None:
        if pts is not None:
            self._pts_count[pid] = self._pts_count.get(pid, 0) + 1
        if dts is not None:
            self._dts_count[pid] = self._dts_count.get(pid, 0) + 1

    def summarize(self) -> tuple[dict[int, PidTiming], list[Finding]]:
        out: dict[int, PidTiming] = {}
        findings: list[Finding] = []
        pids = set(self._pcr) | set(self._pts_count) | set(self._dts_count)
        for pid in sorted(pids):
            t = PidTiming(
                pid=pid,
                pts_samples=self._pts_count.get(pid, 0),
                dts_samples=self._dts_count.get(pid, 0),
            )
            samples = self._pcr.get(pid, [])
            t.pcr_samples = len(samples)
            if len(samples) >= 2:
                values = np.array([s.pcr for s in samples], dtype=np.int64)
                raw_deltas = np.diff(values)
                signaled = np.array(
                    [s.signaled_discontinuity for s in samples[1:]]
                )
                # A negative raw delta is a real backwards PCR unless a
                # discontinuity was signaled. Wrap at 2^42 only for the
                # interval statistics (a legitimate wrap needs ~4.8h of
                # continuous clock and is treated as unwrapped here).
                wrap = 1 << 42
                stat_deltas = np.where(raw_deltas < 0,
                                       raw_deltas + wrap, raw_deltas)
                normal = stat_deltas[~signaled]
                if normal.size:
                    t.pcr_min_interval_27mhz = int(normal.min())
                    t.pcr_mean_interval_27mhz = float(normal.mean())
                    t.pcr_max_interval_27mhz = int(normal.max())
                bad_idx = np.flatnonzero((raw_deltas < 0) & (~signaled))
                t.pcr_non_monotonic = int(bad_idx.size)
                for i in bad_idx:
                    s = samples[int(i) + 1]
                    findings.append(Finding(
                        code=PCR_NON_MONOTONIC,
                        severity=Severity.WARNING,
                        disposition=Disposition.UNDETERMINED,
                        message=f"PCR went backwards on PID {pid:#06x} "
                                "without a discontinuity indicator",
                        packet_index=s.packet_index,
                        pid=pid,
                        details={
                            "pcr": s.pcr,
                            "previous_pcr": samples[int(i)].pcr,
                        }))
            out[pid] = t
        return out, findings
