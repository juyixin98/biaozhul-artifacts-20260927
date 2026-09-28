"""信号内核：双阈值迟滞静音切段状态机。

判定规则（全部以**采样点**为单位，与采样率解耦）：

1. 逐样本按幅度 ``|x|`` 分三带：
   * LOW  ：``|x| < enter_threshold``        —— 足够安静，可计入“进入静音”；
   * LOUD ：``|x| >= exit_threshold``        —— 足够响，可计入“退出静音”；
   * MID  ：``enter_threshold <= |x| < exit_threshold``（enter==exit 时此带为空）
     —— 迟滞带：不推动任何一方，且会冲掉正在累计的进入/退出计数。
2. 进入静音：处于 ACTIVE 时，连续 LOW 样本累计达到 ``min_silence`` 才切换；
   静音区锚点是这段 LOW 的起点（判定用最小持续时间，切段起点不被推迟）。
3. 退出静音：处于 SILENT 时，连续 LOUD 样本累计达到 ``min_activity`` 才切换；
   新区间起点回溯到这段 LOUD 的起点。
   => 长静音中的短噪声（LOUD 长度不足）只会重置计数，不会割裂静音，
      也不会产生伪区间。
4. 结束（finish）时若停在 ACTIVE：未确认成静音的尾部（含 MID/未达标 LOW）
   作为“末尾未完成段”原样保留；停在 SILENT 则不产生尾段。
5. 后处理：对原始区间两端各保留 ``pad`` 个样本，再把相邻间隔
   ``<= merge_gap`` 个样本的区间合并，最后裁到 ``[0, total_samples)``。

跨块不变性：内核只持有游程（run）级状态，输入按什么块大小切分都不影响结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import SegmentError

LABEL_LOW = 0
LABEL_MID = 1
LABEL_LOUD = 2
_LABEL_NAMES = {LABEL_LOW: "low", LABEL_MID: "mid", LABEL_LOUD: "loud"}

_SILENT = "silent"
_ACTIVE = "active"

# 单作业保留的判定事件上限（超出后只保留首尾，防止异常长输入把内存吃光）。
_MAX_EVENTS = 20_000


@dataclass(frozen=True)
class Params:
    """切段参数；时间量在进入内核前已由 timing 内核换算成采样点。"""

    sample_rate: int
    enter_threshold: float
    exit_threshold: float
    min_silence: int
    min_activity: int
    pad: int
    merge_gap: int

    def validate(self) -> None:
        if not isinstance(self.sample_rate, int) or self.sample_rate <= 0:
            raise SegmentError(
                "INVALID_ARGUMENT", "sample_rate must be a positive integer",
                field="sample_rate",
            )
        for name, val in (
            ("enter_threshold", self.enter_threshold),
            ("exit_threshold", self.exit_threshold),
        ):
            if not isinstance(val, (int, float)) or not np.isfinite(val) or val < 0:
                raise SegmentError(
                    "INVALID_ARGUMENT",
                    f"{name} must be a finite non-negative float",
                    field=name,
                )
        if self.enter_threshold > self.exit_threshold:
            raise SegmentError(
                "INVALID_ARGUMENT",
                "enter_threshold must be <= exit_threshold (hysteresis requires "
                "a lower entry bar than exit bar)",
                field="thresholds",
                enter=self.enter_threshold,
                exit=self.exit_threshold,
            )
        for name, val in (
            ("min_silence", self.min_silence),
            ("min_activity", self.min_activity),
            ("pad", self.pad),
            ("merge_gap", self.merge_gap),
        ):
            if not isinstance(val, int) or val < 0:
                raise SegmentError(
                    "INVALID_ARGUMENT",
                    f"{name} must be a non-negative integer number of samples",
                    field=name,
                )


@dataclass(frozen=True)
class SegmentationResult:
    """一次完整切段的结果。

    Attributes:
        intervals: 最终保留区间（半开、含前后保留、合并、裁剪），单位样本。
        raw_ranges: 后处理前的原始有声区间，供核验与追踪。
        total_samples: 原音频样本总数。
        params: 实际使用的参数。
    """

    intervals: list[tuple[int, int]]
    raw_ranges: list[tuple[int, int]]
    total_samples: int
    params: Params


def _classify(chunk: np.ndarray, enter: float, exit_: float) -> np.ndarray:
    """把一个块分成 LOW/MID/LOUD 三类。NaN/Inf 在此之前已被拦截。"""
    mag = np.abs(chunk)
    labels = np.full(chunk.shape[0], LABEL_MID, dtype=np.int8)
    labels[mag < enter] = LABEL_LOW
    labels[mag >= exit_] = LABEL_LOUD
    return labels


def finalize_intervals(
    raw_ranges: list[tuple[int, int]],
    total_samples: int,
    pad: int,
    merge_gap: int,
) -> list[tuple[int, int]]:
    """后处理：加前后保留 -> 裁剪 -> 按 merge_gap 合并 -> 去空。

    合并规则明确：两个（已加保留量、已排序）区间 ``[a,b) [c,d)``，当
    ``c - b <= merge_gap``（负值即重叠）时合并为 ``[a, max(b,d))``。
    """
    if total_samples < 0:
        raise SegmentError(
            "COMPUTATION_FAILED", "total_samples must be non-negative",
            total_samples=total_samples,
        )
    padded = sorted(
        (max(0, s - pad), min(total_samples, e + pad)) for s, e in raw_ranges
    )
    merged: list[list[int]] = []
    for s, e in padded:
        if merged and s - merged[-1][1] <= merge_gap:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e > s]


def validate_intervals(intervals: list[tuple[int, int]], total_samples: int) -> None:
    """防御性不变量校验：区间非空、有序、不重叠、不越界。"""
    prev_end = 0
    for i, (s, e) in enumerate(intervals):
        if not (0 <= s < e <= total_samples):
            raise SegmentError(
                "COMPUTATION_FAILED",
                "interval out of bounds or empty",
                index=i,
                interval=[s, e],
                total_samples=total_samples,
            )
        if s < prev_end:
            raise SegmentError(
                "COMPUTATION_FAILED",
                "intervals overlap or are not sorted",
                index=i,
                interval=[s, e],
                prev_end=prev_end,
            )
        prev_end = e


class SilenceSegmenter:
    """流式双阈值状态机。

    典型用法::

        seg = SilenceSegmenter(params)
        for chunk in stream:
            seg.process(chunk)   # 跨块延续状态
        raw = seg.finish()
        intervals = finalize_intervals(raw, total, params.pad, params.merge_gap)
    """

    def __init__(self, params: Params) -> None:
        params.validate()
        self.p = params
        self.state: str = _SILENT
        self.total_seen = 0

        # —— 跨块：最后一个尚未终止的游程（可能跨多个输入块）——
        self._carry_label: int | None = None
        self._carry_len = 0

        # —— 状态机计数器：记录“当前状态相关游程”的全局起点与累计长度 ——
        self._seg_start: int | None = None          # ACTIVE 段起点
        self._activity_confirmed = False
        self._low_start: int | None = None          # ACTIVE 下进行中的 LOW 游程
        self._low_len = 0
        self._loud_start: int | None = None         # SILENT 下进行中的 LOUD 游程
        self._loud_len = 0

        self._raw_ranges: list[tuple[int, int]] = []
        self._chunk_seq = 0
        self.events: list[dict[str, Any]] = [
            {"event": "init", "state": self.state, "params": _params_event(params)}
        ]

    # ---- 观测 ----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """关键中间状态（写入运行日志，用于复现/重放失败）。"""
        return {
            "state": self.state,
            "total_seen": self.total_seen,
            "carry": None
            if self._carry_label is None
            else {"label": _LABEL_NAMES[self._carry_label], "length": self._carry_len},
            "segment_start": self._seg_start,
            "activity_confirmed": self._activity_confirmed,
            "pending_low": None
            if self._low_start is None
            else {"start": self._low_start, "length": self._low_len},
            "loud_run": None
            if self._loud_start is None
            else {"start": self._loud_start, "length": self._loud_len},
            "raw_ranges": list(self._raw_ranges),
        }

    @property
    def raw_ranges(self) -> list[tuple[int, int]]:
        return list(self._raw_ranges)

    # ---- 输入 ----------------------------------------------------------------

    def process(self, chunk: np.ndarray) -> None:
        """喂入一块样本；块边界不影响判定（游程跨块延续）。"""
        arr = np.asarray(chunk, dtype=np.float64)
        if arr.ndim != 1:
            raise SegmentError(
                "INVALID_ARGUMENT", "samples must be a 1-D array", ndim=arr.ndim
            )
        if arr.size == 0:
            return
        bad = np.flatnonzero(~np.isfinite(arr))
        if bad.size:
            raise SegmentError(
                "COMPUTATION_FAILED",
                "non-finite sample (NaN/Inf) cannot be classified",
                sample_index=self.total_seen + int(bad[0]),
            )

        self._chunk_seq += 1
        prior_total = self.total_seen
        labels = _classify(arr, self.p.enter_threshold, self.p.exit_threshold)
        change = np.flatnonzero(labels[1:] != labels[:-1]) + 1
        bounds = np.concatenate(([0], change, [labels.size]))
        run_labels = labels[bounds[:-1]]
        run_lens = np.diff(bounds)

        self._record(
            {
                "event": "chunk",
                "seq": self._chunk_seq,
                "samples": int(arr.size),
                "range": [prior_total, prior_total + int(arr.size)],
                "runs": int(run_labels.size),
                "state_before": self.state,
            }
        )

        run_start = prior_total
        for label_np, len_np in zip(run_labels, run_lens, strict=True):
            label, length = int(label_np), int(len_np)
            if self._carry_label == label:
                # 同带跨块延续：同一游程，累计长度继续增加。
                self._carry_len += length
            else:
                # 旧游程在上一游程结束时已终止；开启新游程。
                self._carry_label, self._carry_len = label, length
            self._handle_run(
                label,
                run_start,
                run_len=self._carry_len,
                new_len=length,
            )
            run_start += length
        self.total_seen = prior_total + int(arr.size)

    def finish(self) -> list[tuple[int, int]]:
        """结束输入：按当前状态收尾，返回原始区间。

        所有游程在处理时即已增量驱动状态机，这里只处理“末尾未完成段”：
        停在 ACTIVE 则保留到音频末尾；停在 SILENT 不产生尾段。
        """
        if self.state == _ACTIVE and self._activity_confirmed:
            self._emit_range(
                self._seg_start or 0, self.total_seen, reason="trailing_open"
            )
        self._record(
            {
                "event": "finish",
                "total_seen": self.total_seen,
                "raw_ranges": list(self._raw_ranges),
                **self.snapshot(),
            }
        )
        return list(self._raw_ranges)

    # ---- 游程处理（状态可在游程中部切换，递归至多两层） -------------------------

    def _handle_run(
        self,
        label: int,
        run_start: int,
        *,
        run_len: int,
        new_len: int,
        depth: int = 0,
    ) -> None:
        """把一个（可能跨块的）游程本块新增的 ``new_len`` 个样本喂给状态机。

        ``run_start`` 为该游程全局起点，``run_len`` 为含跨块的累计长度。
        阈值满足时在游程内部切换状态，剩余同带样本在新状态下重新计入
        （其作为新状态里的新游程，长度从 1 计）。
        """
        if depth > 2:
            # 不可达：同一批新增样本至多“进入”再“退出”两次。
            raise SegmentError(
                "COMPUTATION_FAILED",
                ">2 mid-run transitions while handling one run",
                label=_LABEL_NAMES[label],
            )

        if self.state == _ACTIVE:
            if label != LABEL_LOW:
                # MID/LOUD 都打断“进入静音”的累计。
                self._low_reset()
                if label == LABEL_LOUD:
                    self._loud_reset()
                return
            # ACTIVE + LOW：设置连续 LOW 计数（游程起点固定，长度累加）。
            if self._low_len == 0:
                self._low_start = run_start
            self._low_len = run_len
            if run_len < self.p.min_silence:
                return
            consumed = self.p.min_silence - (run_len - new_len)
            consumed = max(1, min(consumed, new_len))
            anchor = self._low_start
            self._enter_silence(anchor)
            if consumed < new_len:
                # 剩余 LOW 处于 SILENT：作为 SILENT 下的 LOW 游程重新处理。
                self._carry_len = new_len - consumed
                self._handle_run(
                    LABEL_LOW,
                    run_start + consumed,
                    run_len=new_len - consumed,
                    new_len=new_len - consumed,
                    depth=depth + 1,
                )
            return

        # state == SILENT
        if label != LABEL_LOUD:
            # LOW/MID 都打断“退出静音”的累计 => 短噪声不割裂长静音。
            self._loud_reset()
            return
        if self._loud_len == 0:
            self._loud_start = run_start
        self._loud_len = run_len
        if run_len < self.p.min_activity:
            return
        consumed = self.p.min_activity - (run_len - new_len)
        consumed = max(1, min(consumed, new_len))
        anchor = self._loud_start
        self._exit_silence(anchor)
        if consumed < new_len:
            self._carry_len = new_len - consumed
            self._handle_run(
                LABEL_LOUD,
                run_start + consumed,
                run_len=new_len - consumed,
                new_len=new_len - consumed,
                depth=depth + 1,
            )

    # ---- 状态切换 -------------------------------------------------------------

    def _enter_silence(self, anchor: int) -> None:
        """确认进入静音：闭合当前原始有声区间，尾点为静音锚点（LOW 起点）。"""
        if self._activity_confirmed and self._seg_start is not None:
            self._emit_range(self._seg_start, anchor, reason="silence_confirmed")
        else:
            # 开机至今没有任何被确认的活动：前缀静音丢弃，不产生区间。
            self._record(
                {"event": "drop_prefix", "anchor": anchor,
                 "snapshot": self.snapshot()}
            )
        self.state = _SILENT
        self._seg_start = None
        self._activity_confirmed = False
        self._low_reset()
        self._record({"event": "enter_silence", "anchor": anchor,
                      "snapshot": self.snapshot()})

    def _exit_silence(self, anchor: int) -> None:
        """确认退出静音：新原始区间起点回溯到 LOUD 游程起点。"""
        self.state = _ACTIVE
        self._seg_start = anchor
        self._activity_confirmed = True
        self._loud_reset()
        self._low_reset()
        self._record({"event": "exit_silence", "anchor": anchor,
                      "snapshot": self.snapshot()})

    # ---- 小工具 ---------------------------------------------------------------

    def _emit_range(self, start: int, end: int, *, reason: str) -> None:
        if end <= start:
            return
        self._raw_ranges.append((start, end))
        self._record(
            {"event": "raw_range", "start": start, "end": end, "reason": reason}
        )

    def _low_reset(self) -> None:
        self._low_start = None
        self._low_len = 0

    def _loud_reset(self) -> None:
        self._loud_start = None
        self._loud_len = 0

    def _record(self, event: dict[str, Any]) -> None:
        if len(self.events) < _MAX_EVENTS:
            self.events.append(event)


def _params_event(p: Params) -> dict[str, Any]:
    return {
        "sample_rate": p.sample_rate,
        "enter_threshold": p.enter_threshold,
        "exit_threshold": p.exit_threshold,
        "min_silence": p.min_silence,
        "min_activity": p.min_activity,
        "pad": p.pad,
        "merge_gap": p.merge_gap,
    }


def segment_samples(
    samples: np.ndarray,
    params: Params,
    *,
    chunk_size: int | None = None,
) -> SegmentationResult:
    """一次性便捷封装：按 ``chunk_size`` 分块喂入再 finish + 后处理。

    ``chunk_size`` 仅用于模拟流式分块；结果必须与分块大小无关（切块不变性）。
    """
    arr = np.asarray(samples, dtype=np.float64)
    seg = SilenceSegmenter(params)
    if chunk_size is None or chunk_size >= arr.size:
        seg.process(arr)
    else:
        for start in range(0, arr.size, chunk_size):
            seg.process(arr[start : start + chunk_size])
    raw = seg.finish()
    intervals = finalize_intervals(raw, arr.size, params.pad, params.merge_gap)
    validate_intervals(intervals, arr.size)
    return SegmentationResult(
        intervals=intervals,
        raw_ranges=raw,
        total_samples=arr.size,
        params=params,
    )
