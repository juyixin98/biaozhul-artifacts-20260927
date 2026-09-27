"""呈现时间线内核：把一轨的样本表 + edit list 映射为电影时间轴上的播放区间。

算法（假设见 README）：

1. 每个样本有 DTS（解码顺序累计）与 PTS = DTS + CTO（CTO 有符号）。
2. 无 edit list 时按恒等编辑处理：整段媒体从电影时间 0 开始。
3. 顺序处理每条 edit，维护电影时间游标 ``movie_cursor``：
   - 空编辑（media_time == -1）：游标前进 segment_duration，不呈现样本
     （电影时间轴上形成一段空档）；
   - 普通编辑：媒体区间 [media_time, media_time + seg_media_dur)，其中
     seg_media_dur = segment_duration * media_timescale / movie_timescale
     （有理数换算）。凡 PTS 落在该区间内的样本被呈现：

         movie_time = movie_cursor
                      + (PTS - media_time) * movie_timescale / media_timescale

     呈现时长按同一比例换算；样本末端越过段尾时在段边界裁剪。
4. 区间边界规则：起点闭、终点开 —— PTS == 段起点 的样本被包含，
   PTS == 段终点 的样本不被包含（留给下一段或丢弃）。
5. 未被任何编辑覆盖的样本记为 unpresented（带原因），不静默丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from ..mp4parse.editlist import Edit
from ..mp4parse.parser import Track
from ..mp4parse.sample_table import Sample
from .rational import format_fraction, rescale, to_seconds


@dataclass(frozen=True)
class Presentation:
    """一个样本在电影时间轴上的呈现记录。时间均为精确分数。"""

    sample_index: int
    dts: int  # media timescale
    cto: int  # 有符号
    pts: int  # media timescale
    duration_media: int  # media timescale
    movie_time: Fraction  # movie timescale 单位
    movie_duration: Fraction  # movie timescale 单位（段尾裁剪后）
    byte_offset: int
    size: int
    is_sync: bool
    edit_index: int  # 覆盖它的 edit 序号


@dataclass(frozen=True)
class UnpresentedSample:
    sample_index: int
    dts: int
    cto: int
    pts: int
    reason: str


@dataclass(frozen=True)
class TrackTimeline:
    track_id: int
    handler: str
    media_timescale: int
    movie_timescale: int
    presentations: tuple[Presentation, ...]  # 按电影时间升序
    unpresented: tuple[UnpresentedSample, ...]
    movie_span: tuple[Fraction, Fraction]  # 该轨在电影轴上的 [起点, 终点)

    # ---- 便捷换算（供 API 层序列化） ----
    def presentation_view(self, p: Presentation) -> dict:
        return {
            "sample_index": p.sample_index,
            "dts": p.dts,
            "cto": p.cto,
            "pts": p.pts,
            "pts_seconds": format_fraction(to_seconds(p.pts, self.media_timescale)),
            "duration_media": p.duration_media,
            "movie_time": format_fraction(p.movie_time),
            "movie_time_seconds": format_fraction(
                to_seconds(p.movie_time, self.movie_timescale)
            ),
            "movie_duration": format_fraction(p.movie_duration),
            "playback_interval_seconds": [
                format_fraction(to_seconds(p.movie_time, self.movie_timescale)),
                format_fraction(
                    to_seconds(p.movie_time + p.movie_duration, self.movie_timescale)
                ),
            ],
            "byte_range": [p.byte_offset, p.size],
            "is_sync": p.is_sync,
            "edit_index": p.edit_index,
        }


def _identity_edit(track: Track, movie_timescale: int) -> Edit:
    """无 edit list 时的恒等编辑：整段媒体映射到电影轴原点。"""

    seg_dur = rescale(track.media_duration, track.media_timescale, movie_timescale)
    if seg_dur.denominator != 1:
        # 恒等编辑时长必须是整数（movie 单位）；不能整除时向上取整，
        # 多出的尾部没有样本，等价于段尾裁剪，不影响任何样本。
        seg_dur = Fraction(seg_dur.numerator // seg_dur.denominator + 1)
    return Edit(int(seg_dur), 0, 1, 0)


def build_track_timeline(track: Track, movie_timescale: int) -> TrackTimeline:
    edits: tuple[Edit, ...] = track.edits or (_identity_edit(track, movie_timescale),)

    # 按 PTS 升序遍历样本（稳定排序：PTS 相同保持解码顺序）。
    pts_array = np.asarray([s.pts for s in track.samples], dtype=np.int64)
    pts_order = np.argsort(pts_array, kind="stable")
    ordered: list[Sample] = [track.samples[int(i)] for i in pts_order]

    presented: dict[int, Presentation] = {}
    presentations: list[Presentation] = []
    movie_cursor = Fraction(0)

    for edit_index, edit in enumerate(edits):
        if edit.is_empty:
            movie_cursor += edit.segment_duration
            continue

        seg_media_start = Fraction(edit.media_time)
        seg_media_dur = rescale(
            edit.segment_duration, movie_timescale, track.media_timescale
        )
        seg_media_end = seg_media_start + seg_media_dur

        for sample in ordered:
            pts = Fraction(sample.pts)
            if pts < seg_media_start or pts >= seg_media_end:
                continue  # 起点闭、终点开
            if sample.index in presented:
                continue  # 先出现的编辑优先；重复覆盖不重复呈现
            sample_end = min(pts + sample.duration, seg_media_end)
            movie_time = movie_cursor + rescale(
                pts - seg_media_start, track.media_timescale, movie_timescale
            )
            movie_duration = rescale(
                sample_end - pts, track.media_timescale, movie_timescale
            )
            presentations.append(
                Presentation(
                    sample_index=sample.index,
                    dts=sample.dts,
                    cto=sample.cto,
                    pts=sample.pts,
                    duration_media=sample.duration,
                    movie_time=movie_time,
                    movie_duration=movie_duration,
                    byte_offset=sample.byte_offset,
                    size=sample.size,
                    is_sync=sample.is_sync,
                    edit_index=edit_index,
                )
            )
            presented[sample.index] = presentations[-1]

        movie_cursor += edit.segment_duration

    unpresented = tuple(
        UnpresentedSample(
            sample_index=s.index,
            dts=s.dts,
            cto=s.cto,
            pts=s.pts,
            reason="样本 PTS 未被任何 edit 段覆盖",
        )
        for s in track.samples
        if s.index not in presented
    )

    presentations.sort(key=lambda p: (p.movie_time, p.sample_index))
    if presentations:
        span_start = min(p.movie_time for p in presentations)
        span_end = max(p.movie_time + p.movie_duration for p in presentations)
    else:
        span_start = span_end = Fraction(0)

    return TrackTimeline(
        track_id=track.track_id,
        handler=track.handler,
        media_timescale=track.media_timescale,
        movie_timescale=movie_timescale,
        presentations=tuple(presentations),
        unpresented=unpresented,
        movie_span=(span_start, span_end),
    )
