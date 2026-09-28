"""时间与信号内核：双阈值滞回（hysteresis）静音切段。

输入: 一维 float64 单声道样本（幅度，|x|<=1），可分任意多次 push。
输出: 保留区间 [Interval(start, end), ...]，半开区间，按帧（样本）序号计。

状态机（原始运行段 raw run，标签 S/H）:
    S 侧: level >= exit_threshold 才翻 H; [enter, exit) 中间带保持 S
    H 侧: level <  enter_threshold 才翻 S; [enter, exit) 中间带保持 H

二级判定（对闭合 run 独立打标签，再合并同标签邻段）:
    H 且 length >= min_speech  -> speech; 否则短噪声 -> silence
    S 且 length >= min_silence -> silence; 否则短静缝 -> speech
    edge_keep=True 时，与流首/流尾相接的原始 H 段即使不足 min_speech 也保留。

区间: 语音区域 -> 加 pad_before/pad_after -> merge_gap 邻接合并 -> clamp。

切块不变性: push 的切块方式不影响 finish() 的结果。流式过程中仅提交
"锁定"区间（见 _pump_locked）。一个已观测长度 >= lock_length 的静音间隙
[g0,g1) 给出安全边界 B = g1 - pad_before - merge_gap：
左侧区间 pad 后 end <= g0 + pad_after < B（长度差保证），右侧任何未来
区间 start >= g1 - pad_before = B + merge_gap，既不会改动也不会在收尾时
跨过 B 合并。故 end <= B 的区间可提前提交且永不改变。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

# ---------------------------------------------------------------------------
# 配置与数据类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentConfig:
    sample_rate: int
    enter_threshold: float  # H -> S 边界（下限阈值）
    exit_threshold: float   # S -> H 边界（上限阈值）
    min_speech: int         # H 段最短语音样本数
    min_silence: int        # S 段最短静音样本数
    pad_before: int         # 区间前保留量（样本）
    pad_after: int          # 区间后保留量（样本）
    merge_gap: int          # 区间间隔 <= 该值则合并（样本）
    edge_keep: bool         # 是否保留流首/流尾未确认 H 段

    def __post_init__(self) -> None:
        et, xt = self.enter_threshold, self.exit_threshold
        if not (math.isfinite(et) and math.isfinite(xt)):
            raise ValueError("thresholds must be finite")
        if not (0.0 <= et < xt <= 1.0):
            raise ValueError(
                "require 0 <= enter_threshold < exit_threshold <= 1 "
                f"(got enter={et}, exit={xt})")
        for name in ("min_speech", "min_silence", "pad_before",
                     "pad_after", "merge_gap"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if not isinstance(self.sample_rate, int) or self.sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")

    @property
    def lock_length(self) -> int:
        """确认一个静音间隙足以锁定其左侧区间所需的最小观测长度。"""
        return max(self.min_silence,
                   self.pad_before + self.pad_after + self.merge_gap + 1)


@dataclass(frozen=True)
class Run:
    """状态机原始运行段 [start, end)。"""
    start: int
    end: int
    raw: str  # 'S' 静音侧 / 'H' 语音侧

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Interval:
    """保留区间 [start, end)。"""
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"bad interval: {self.start}..{self.end}")


EventCallback = Callable[[dict], None]


# ---------------------------------------------------------------------------
# 纯函数：标签判定与区间构建
# ---------------------------------------------------------------------------


def classify_runs(runs: list[Run], cfg: SegmentConfig, edge: bool) -> list[str]:
    """对 runs 打二级标签，返回等长的 'speech'/'silence' 列表。

    edge=True 表示 runs 覆盖完整流（首尾 run 与流边界相接），应用 edge_keep。
    标签仅依赖 run 自身长度、原始标签、位置和配置，与邻居无关；同标签邻段
    的合并在 intervals_from_labels 中完成。
    """
    labels: list[str] = []
    last = len(runs) - 1
    for i, r in enumerate(runs):
        at_edge = edge and (i == 0 or i == last)
        if r.raw == "H":
            if r.length >= cfg.min_speech or (at_edge and cfg.edge_keep):
                labels.append("speech")
            else:
                labels.append("silence")  # 短噪声：不割裂两侧静音
        else:
            labels.append(
                "silence" if r.length >= cfg.min_silence
                else "speech")           # 短静缝：两侧语音相连
    return labels


def intervals_from_labels(runs: list[Run], labels: list[str], total: int,
                          cfg: SegmentConfig) -> list[Interval]:
    """合并连续 speech run -> pad -> merge_gap 邻接合并 -> clamp。

    一个语音区域必须包含至少一个 raw=H 的 run：纯 S run 被标 speech 只是
    "短静缝桥接"，不能在从无 H 的流（全程未越过 exit 阈值）里凭空造出区间。
    """
    regions: list[tuple[int, int]] = []
    i, n = 0, len(runs)
    while i < n:
        if labels[i] != "speech":
            i += 1
            continue
        j = i
        while j < n and labels[j] == "speech":
            j += 1
        if any(runs[k].raw == "H" for k in range(i, j)):
            regions.append((runs[i].start, runs[j - 1].end))
        i = j

    out: list[Interval] = []
    for a, b in regions:
        iv = Interval(max(0, a - cfg.pad_before),
                      min(total, b + cfg.pad_after))
        if out and iv.start <= out[-1].end + cfg.merge_gap:
            out[-1] = Interval(out[-1].start, max(out[-1].end, iv.end))
        else:
            out.append(iv)
    return out


def build_intervals(runs: list[Run], total: int, cfg: SegmentConfig,
                    edge: bool) -> list[Interval]:
    return intervals_from_labels(
        runs, classify_runs(runs, cfg, edge=edge), total, cfg)


# ---------------------------------------------------------------------------
# 流式切段器
# ---------------------------------------------------------------------------


class StreamingSegmenter:
    """跨块延续状态的流式切段器。

    - push():    写入一块一维 float64 样本；状态与未闭合 run 跨块延续。
    - finish():  收尾，应用流首尾 edge 规则，返回与切块方式无关的最终区间。
    - locked_intervals:     已锁定（未来不可能再变）的区间，可提前落库。
    - tentative_intervals:  基于当前观测的试算区间（含未锁定尾部，可能变化）。
    """

    def __init__(self, cfg: SegmentConfig,
                 on_event: Optional[EventCallback] = None) -> None:
        self.cfg = cfg
        self._on_event = on_event
        self._state_in_chunk = "S"   # 当前块开始时的状态机状态
        self._state = "S"            # 最新样本后的状态（开流假定静音侧）
        self._total = 0
        self._runs: list[Run] = []   # 最后一个 run 可能未闭合
        self._locked: list[Interval] = []
        self._safe_boundary = 0      # 已锁定到的样本位置 B（单调递增）
        self._announced: set[tuple] = set()
        self._finished = False

    @property
    def total_samples(self) -> int:
        return self._total

    @property
    def runs(self) -> list[Run]:
        return list(self._runs)

    @property
    def locked_intervals(self) -> list[Interval]:
        return list(self._locked)

    @property
    def tentative_intervals(self) -> list[Interval]:
        """基于当前观测的试算区间。

        流首 run 的 edge 命运在开流时即确定（永远是 runs[0]），故流中期也
        应用；流尾 open run 不应用 edge，其最终标签要等 finish。
        """
        return self._tentative()

    # -- 写入 --------------------------------------------------------------

    def push(self, x: np.ndarray) -> None:
        if self._finished:
            raise RuntimeError("push after finish")
        x = np.asarray(x, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError("samples must be 1-D")
        if x.size == 0:
            return
        if not np.all(np.isfinite(x)):
            raise ValueError("samples contain non-finite values")

        chunk_start = self._total
        states = self._classify_chunk(np.abs(x))
        self._emit({"type": "chunk", "start": chunk_start,
                    "end": chunk_start + x.size, "frames": int(x.size),
                    "state_in": self._state_in_chunk,
                    "state_out": self._state})
        self._absorb(states, x.size)
        self._total += x.size
        self._announce_closed_runs()
        self._pump_locked()

    def finish(self) -> list[Interval]:
        if self._finished:
            return list(self._locked)
        self._finished = True
        final = build_intervals(self._runs, self._total, self.cfg, edge=True)
        k = len(self._locked)
        if final[:k] != self._locked:
            raise RuntimeError(
                "streaming lock mismatch: locked prefix does not match final "
                f"intervals; locked={self._locked} final={final}")
        for iv in final[k:]:
            self._locked.append(iv)
            self._emit({"type": "commit_final", "start": iv.start,
                        "end": iv.end,
                        "reason": "stream finished; stream-tail edge rule "
                        "applied and interval clamped to stream bounds"})
        self._safe_boundary = self._total
        self._emit({"type": "finished", "total_samples": self._total,
                    "num_intervals": len(self._locked)})
        return list(self._locked)

    # -- 状态机分块（跨块延续） -------------------------------------------

    def _classify_chunk(self, level: np.ndarray) -> np.ndarray:
        """返回该块每样本状态（0=S,1=H），初始状态取上一块结束时状态。"""
        n = level.shape[0]
        out = np.empty(n, dtype=np.int8)
        hi_idx = np.flatnonzero(level >= self.cfg.exit_threshold)
        lo_idx = np.flatnonzero(level < self.cfg.enter_threshold)
        pos, s = 0, 0 if self._state == "S" else 1
        self._state_in_chunk = self._state
        while pos < n:
            idx = hi_idx if s == 0 else lo_idx
            rel = idx[idx >= pos]
            j = int(rel[0]) if rel.size else n
            out[pos:j] = s
            if j < n:
                s = 1 - s  # 中间带样本保持原状态；只在跨过对侧阈值时翻转
            pos = j
        self._state = "H" if s == 1 else "S"
        return out

    def _absorb(self, states: np.ndarray, size: int) -> None:
        """把块状态 RLE 拼接到 runs；块首同状态则延续未闭合 run。"""
        change = np.flatnonzero(np.diff(states.astype(np.int16))) + 1
        bounds = np.concatenate(([0], change, [size]))
        raw = ["S", "H"]
        for k in range(bounds.shape[0] - 1):
            b0, b1 = int(bounds[k]), int(bounds[k + 1])
            r = raw[int(states[b0])]
            start, end = self._total + b0, self._total + b1
            if self._runs and self._runs[-1].raw == r and \
                    self._runs[-1].end == start:
                prev = self._runs[-1]
                self._runs[-1] = Run(prev.start, end, r)
            else:
                if self._runs and self._runs[-1].end != start:
                    raise RuntimeError("internal: non-contiguous runs")
                self._runs.append(Run(start, end, r))
                self._emit({"type": "run_open", "start": start, "raw": r,
                            "reason": "level crossed "
                                      + ("exit_threshold (S->H)"
                                         if r == "H"
                                         else "enter_threshold (H->S)")})

    def _announce_closed_runs(self) -> None:
        """对新闭合的 run 发判定理由事件（open run 等 finish）。"""
        for i, r in enumerate(self._runs[:-1]):
            key = (r.start, r.end, r.raw)
            if key in self._announced:
                continue
            self._announced.add(key)
            label = self._closed_label(i, r)
            if r.raw == "H":
                if label == "silence":
                    why = (f"H run {r.start}..{r.end} length={r.length} "
                           f"< min_speech={self.cfg.min_speech}; short noise, "
                           "absorbed by surrounding silence")
                else:
                    why = (f"H run {r.start}..{r.end} length={r.length} "
                           f">= min_speech={self.cfg.min_speech}; speech"
                           + (" (stream-head edge keep)"
                              if i == 0 and r.length < self.cfg.min_speech
                              else ""))
            else:
                why = (f"S run {r.start}..{r.end} length={r.length} "
                       + (f">= min_silence={self.cfg.min_silence}; silence "
                          "confirmed, may cut here"
                          if label == "silence"
                          else f"< min_silence={self.cfg.min_silence}; short "
                          "gap bridged, speech regions stay joined"))
            self._emit({"type": "run_closed", "start": r.start,
                        "end": r.end, "raw": r.raw, "length": r.length,
                        "label": label, "reason": why})

    def _closed_label(self, i: int, r: Run) -> str:
        if r.raw == "H":
            if r.length >= self.cfg.min_speech or \
                    (i == 0 and self.cfg.edge_keep):
                return "speech"
            return "silence"
        return "silence" if r.length >= self.cfg.min_silence else "speech"

    # -- 试算与锁定 --------------------------------------------------------

    def _tentative(self) -> list[Interval]:
        if not self._runs:
            return []
        labels = classify_runs(self._runs, self.cfg, edge=False)
        if self.cfg.edge_keep and self._runs[0].raw == "H":
            labels[0] = "speech"  # 流首 edge 命运已确定
        return intervals_from_labels(
            self._runs, labels, self._total, self.cfg)

    def _candidate_boundary(self) -> int:
        """所有已确认的长静音间隙给出的最大安全边界 B。

        - open S 间隙已观测长度 >= lock_length；
        - 闭合 S 间隙长度 >= lock_length。
        """
        if not self._runs:
            return self._safe_boundary
        candidates: list[int] = []
        last = self._runs[-1]
        if last.raw == "S":
            gap_len = self._total - last.start
            if gap_len >= self.cfg.lock_length:
                candidates.append(
                    self._total - self.cfg.pad_before - self.cfg.merge_gap)
        for r in self._runs[:-1]:
            if r.raw == "S" and r.length >= self.cfg.lock_length:
                candidates.append(
                    r.end - self.cfg.pad_before - self.cfg.merge_gap)
        if not candidates:
            return self._safe_boundary
        return max(self._safe_boundary, max(candidates))

    def _pump_locked(self) -> None:
        b = self._candidate_boundary()
        if b <= self._safe_boundary:
            return
        tentative = self._tentative()
        k = len(self._locked)
        if tentative[:k] != self._locked:
            raise RuntimeError(
                "internal: tentative prefix diverged from locked intervals "
                f"locked={self._locked} tentative={tentative}")
        added = False
        for iv in tentative[k:]:
            if iv.end > b:
                break
            self._locked.append(iv)
            added = True
            self._emit({"type": "commit", "start": iv.start, "end": iv.end,
                        "safe_boundary": b,
                        "reason": f"silence gap >= lock_length="
                                  f"{self.cfg.lock_length} observed; padded "
                                  "interval cannot touch future speech"})
        self._safe_boundary = b

    # -- 事件 --------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        if self._on_event is not None:
            self._on_event(event)
