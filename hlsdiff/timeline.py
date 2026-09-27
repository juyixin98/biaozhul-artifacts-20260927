"""时间与信号内核。

三个维度分别维护,互不推导:
- 媒体序号(media sequence):分段的标识维度,来自播放列表序号;
- discontinuity 序号:信号维度,标识编码/时间线不连续的分段组;
- 时间线:累积时长维度,由 NumPy 前缀和构建,只用于展示与计划,不回写序号。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .models import Playlist


@dataclass(frozen=True)
class Timeline:
    sequences: np.ndarray  # int64, 媒体序号
    discontinuity_sequences: np.ndarray  # int64, 每分段的 discontinuity 序号
    durations: np.ndarray  # float64, 每分段时长
    starts: np.ndarray  # float64, 每分段在时间线上的起点

    @property
    def total_duration(self) -> float:
        return float(self.durations.sum())

    def index_of(self, sequence: int) -> int | None:
        """按媒体序号定位下标;不存在返回 None。"""
        hits = np.nonzero(self.sequences == sequence)[0]
        return int(hits[0]) if hits.size else None

    def boundary_indices(self) -> np.ndarray:
        """discontinuity 序号发生变化的分段下标(即连续播放边界)。"""
        if self.discontinuity_sequences.size < 2:
            return np.array([], dtype=np.int64)
        change = self.discontinuity_sequences[1:] != self.discontinuity_sequences[:-1]
        return np.nonzero(change)[0] + 1


def build_timeline(playlist: Playlist) -> Timeline:
    """从播放列表构建时间线。空列表得到全空数组。"""
    n = len(playlist.segments)
    sequences = np.array([s.sequence for s in playlist.segments], dtype=np.int64)
    dseqs = np.array(
        [s.discontinuity_sequence for s in playlist.segments], dtype=np.int64
    )
    durations = np.array([s.duration for s in playlist.segments], dtype=np.float64)
    if n:
        starts = np.concatenate(([0.0], np.cumsum(durations)[:-1]))
    else:
        starts = np.array([], dtype=np.float64)
    return Timeline(
        sequences=sequences,
        discontinuity_sequences=dseqs,
        durations=durations,
        starts=starts,
    )
