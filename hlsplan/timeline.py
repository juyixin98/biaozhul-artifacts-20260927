"""时间与信号内核：由分段时长推导时间线、连续播放边界与缺段。

媒体序号 / discontinuity 序号在此不修改，只读取；
时间线（起播时刻）一律由本模块按时长前缀和推导，单一事实来源。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from .models import Segment


def start_times(durations: np.ndarray) -> np.ndarray:
    """时长数组 -> 各分段起播时刻（排他前缀和）。

    例：[4, 4, 6] -> [0, 4, 8]
    """
    durations = np.asarray(durations, dtype=np.float64)
    out = np.zeros_like(durations)
    if durations.size:
        out[1:] = np.cumsum(durations)[:-1]
    return out


def find_missing_sequences(segments: List[Segment]) -> List[int]:
    """窗口内不可用的分段序号（EXT-X-GAP 占位）。"""
    return [s.media_sequence for s in segments if s.gap]


@dataclass
class PlaybackRun:
    """一段连续播放区间：序号连续且未跨 discontinuity。"""

    start_sequence: int
    end_sequence: int
    discontinuity_sequence: int
    start_time: float
    end_time: float
    segment_count: int

    def to_dict(self) -> dict:
        return {
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "discontinuity_sequence": self.discontinuity_sequence,
            "start_time": round(self.start_time, 6),
            "end_time": round(self.end_time, 6),
            "segment_count": self.segment_count,
        }


def continuous_runs(segments: List[Segment]) -> List[PlaybackRun]:
    """把分段序列切成连续播放区间。

    断点条件（满足其一即切分）：
    - 媒体序号不连续；
    - 分段带 EXT-X-GAP（占位但不可用，本身不参与任何区间）；
    - 分段带 discontinuity_before（discontinuity 序号变化）。
    时间线在各区间内按区间起点重新归零——跨 discontinuity 的
    时间轴不可直接拼接，这正是"时间线分别维护"的含义。
    """
    segments = [s for s in segments if not s.gap]
    if not segments:
        return []
    runs: List[PlaybackRun] = []
    group: List[Segment] = [segments[0]]
    for prev, cur in zip(segments, segments[1:]):
        if (
            cur.media_sequence != prev.media_sequence + 1
            or cur.discontinuity_before
            or cur.discontinuity_sequence != prev.discontinuity_sequence
        ):
            runs.append(_make_run(group))
            group = [cur]
        else:
            group.append(cur)
    runs.append(_make_run(group))
    return runs


def _make_run(group: List[Segment]) -> PlaybackRun:
    durations = np.array([s.duration for s in group], dtype=np.float64)
    starts = start_times(durations)
    return PlaybackRun(
        start_sequence=group[0].media_sequence,
        end_sequence=group[-1].media_sequence,
        discontinuity_sequence=group[0].discontinuity_sequence,
        start_time=float(starts[0]),
        end_time=float(starts[-1] + durations[-1]),
        segment_count=len(group),
    )
