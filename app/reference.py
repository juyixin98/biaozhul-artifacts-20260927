"""独立参考实现与区间完整性校验。

刻意不调用 app.kernel 的任何函数：状态机用逐样本 Python 循环、run 聚合与
区间生成都独立书写，只共享 SegmentConfig 这个*配置*数据类。它有两个用途：

1. /jobs/{id}/verify 复核接口的语义 oracle；
2. tests/test_reference_crosscheck.py 用随机信号交叉核对被测内核。

该参考自身的正确性由 tests/test_kernel_hand_verified.py 中的手算夹具锁定
（同一份期望区间同时断言内核与参考）。
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .kernel import Interval, SegmentConfig


def reference_segment(x: Sequence[float], cfg: SegmentConfig,
                      edge: bool = True) -> list[Interval]:
    """逐样本状态机 + 直接构造区间，返回与 build_intervals 相同的契约。"""
    n = len(x)
    if n == 0:
        return []

    # --- 逐样本原始状态（0=S, 1=H），显式 if 循环，不做向量化/RLE 技巧 ---
    raw: list[int] = [0] * n
    state = 0
    for i in range(n):
        level = abs(float(x[i]))
        if state == 0:
            if level >= cfg.exit_threshold:
                state = 1
        else:
            if level < cfg.enter_threshold:
                state = 0
        raw[i] = state

    # --- 聚合成 (起始, 结束, 'H'/'S')，独立书写的 RLE ---
    spans: list[list] = []
    span_start = 0
    for i in range(1, n):
        if raw[i] != raw[i - 1]:
            spans.append([span_start, i, "H" if raw[i - 1] else "S"])
            span_start = i
    spans.append([span_start, n, "H" if raw[n - 1] else "S"])

    # --- 二级标签（独立 if 链） ---
    is_speech: list[bool] = []
    for idx, (a, b, kind) in enumerate(spans):
        at_edge = edge and (idx == 0 or idx == len(spans) - 1)
        if kind == "H":
            keep = (b - a) >= cfg.min_speech or (at_edge and cfg.edge_keep)
            is_speech.append(keep)
        else:
            is_speech.append((b - a) < cfg.min_silence)

    # --- 从语音 span 直接长出区间，不经过"语音区域"中间结构 ---
    # 规则（独立书写）：一个连续 speech span 组只有其中至少含一个 H span
    # 才形成区间——纯 S 的 speech 标签只表示"短静缝桥接"，从无 H 的流
    # （全程没越过 exit 阈值）不得产出区间。
    intervals: list[Interval] = []
    idx = 0
    while idx < len(spans):
        if not is_speech[idx]:
            idx += 1
            continue
        j = idx
        contains_h = False
        while j < len(spans) and is_speech[j]:
            contains_h = contains_h or spans[j][2] == "H"
            j += 1
        if not contains_h:
            idx = j
            continue
        a = max(0, spans[idx][0] - cfg.pad_before)
        b = min(n, spans[j - 1][1] + cfg.pad_after)
        iv = Interval(a, b)
        if intervals and iv.start <= intervals[-1].end + cfg.merge_gap:
            intervals[-1] = Interval(
                intervals[-1].start, max(intervals[-1].end, iv.end))
        else:
            intervals.append(iv)
        idx = j
    return intervals


def validate_intervals(intervals: list[Interval],
                       total: int) -> dict:
    """完整性校验：有序、半开、不重叠、不越界；返回统计与违规列表。"""
    violations: list[str] = []
    union = 0
    prev_end = 0
    for k, iv in enumerate(intervals):
        if iv.start < 0:
            violations.append(f"interval[{k}].start={iv.start} < 0")
        if iv.end > total:
            violations.append(
                f"interval[{k}].end={iv.end} > total={total}")
        if iv.start >= iv.end:
            violations.append(
                f"interval[{k}] empty/reversed: {iv.start}..{iv.end}")
        if k > 0 and iv.start < prev_end:
            violations.append(
                f"interval[{k}] overlaps previous: {iv.start} < {prev_end}")
        union += iv.end - iv.start
        prev_end = iv.end
    return {
        "total_samples": total,
        "num_intervals": len(intervals),
        "kept_samples": union,
        "dropped_samples": total - union,
        "violations": violations,
        "ok": not violations and 0 <= union <= total,
    }
