"""切块不变性、样本守恒、区间完整性、流式锁定的前置一致性。

这些用例不依赖具体期望值（期望值在手算测试与参考交叉测试里锁死），
而是断言*结构性性质*，并对大量随机切块方案穷举。
"""
from __future__ import annotations

import itertools

import numpy as np
import pytest

from app.kernel import (Interval, SegmentConfig, StreamingSegmenter,
                        build_intervals)
from app.reference import validate_intervals


def cfg(**kw) -> SegmentConfig:
    base = dict(sample_rate=1000, enter_threshold=0.03,
                exit_threshold=0.08, min_speech=50, min_silence=100,
                pad_before=10, pad_after=20, merge_gap=0, edge_keep=True)
    base.update(kw)
    return SegmentConfig(**base)


def pairlist(ivs):
    return [(iv.start, iv.end) for iv in ivs]


def feed(x, c, cut):
    seg = StreamingSegmenter(c)
    states = []
    for a, b in cut:
        seg.push(x[a:b])
        states.append((seg.total_samples,
                       pairlist(seg.locked_intervals)))
    final = pairlist(seg.finish())
    return final, states, seg


def all_cuts(n):
    """一组有代表性的切块方案，含全部可能造成状态延续 bug 的切点。"""
    yield [(0, n)]
    for w in (1, 2, 3, 5, 7, 11, 13, 17, 31, 64, 100, 127, 257,
              max(1, n // 2), max(1, n // 3) + 1):
        if w >= n:
            continue
        yield [(i, min(i + w, n)) for i in range(0, n, w)]
    # 切点故意落在常见 run/pad 边界附近
    if n > 200:
        yield [(0, 50), (50, 100), (100, 200), (200, n)]


@pytest.mark.parametrize("seed", range(40))
def test_chunking_invariance_random_signals(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(1, 900))
    # 0/中间带/高 三电平 + 偶发的连续段，产生丰富 run 结构
    levels = rng.choice([0.0, 0.01, 0.05, 0.2, 0.9], size=n,
                        p=[.45, .1, .1, .1, .25])
    # 平滑成段：随机长度的恒定块
    x = np.zeros(n)
    i = 0
    while i < n:
        L = int(rng.integers(1, 80))
        x[i:i + L] = levels[i] if i < n else 0.0
        i += L
    c = cfg()
    whole_seg = StreamingSegmenter(c)
    whole_seg.push(x)
    expected = pairlist(whole_seg.finish())
    for cut in list(all_cuts(n)):
        final, _states, _ = feed(x, c, cut)
        assert final == expected, (seed, [len(z) for z in cut])


@pytest.mark.parametrize("edge_keep", [True, False])
@pytest.mark.parametrize("seed", range(20))
def test_chunking_invariance_edge_modes(seed, edge_keep):
    rng = np.random.default_rng(1000 + seed)
    n = 500
    x = np.zeros(n)
    i = 0
    while i < n:
        L = int(rng.integers(1, 60))
        x[i:i + L] = rng.choice([0.0, 0.05, 0.9], p=[.5, .2, .3])
        i += L
    c = cfg(edge_keep=edge_keep)
    whole_seg = StreamingSegmenter(c)
    whole_seg.push(x)
    expected = pairlist(whole_seg.finish())
    for cut in list(all_cuts(n)):
        final, _, _ = feed(x, c, cut)
        assert final == expected


@pytest.mark.parametrize("seed", range(30))
def test_interval_integrity_and_conservation(seed):
    """任意随机信号 + 随机配置：不重叠、不越界、kept+dropped=total。"""
    rng = np.random.default_rng(2000 + seed)
    n = int(rng.integers(0, 800))
    x = rng.choice([0.0, 0.02, 0.05, 0.5], size=n)
    c = cfg(min_speech=int(rng.integers(1, 80)),
            min_silence=int(rng.integers(1, 120)),
            pad_before=int(rng.integers(0, 40)),
            pad_after=int(rng.integers(0, 40)),
            merge_gap=int(rng.integers(0, 10)),
            edge_keep=bool(rng.integers(0, 2)))
    seg = StreamingSegmenter(c)
    seg.push(x)
    ivs = seg.finish()
    report = validate_intervals(ivs, n)
    assert report["ok"], (seed, report["violations"])
    assert report["kept_samples"] + report["dropped_samples"] == n
    # 合并后区间并集样本数应等于 report 统计（无重叠才成立）
    union = sum(b - a for a, b in pairlist(ivs))
    assert union == report["kept_samples"]
    # 全程锁定路径上的区间同样必须合法（不能提前提交越界区间）
    cut = [(i, min(i + 13, n)) for i in range(0, n, 13)]
    _, states, _ = feed(x, c, cut)
    for total, locked in states:
        rep = validate_intervals([Interval(a, b) for a, b in locked], total)
        assert rep["ok"], (seed, total, rep["violations"])
        assert all(b <= total for a, b in locked)


def test_locked_is_prefix_of_final():
    """流式提前提交的区间必须是最终区间的严格有序前缀。"""
    rng = np.random.default_rng(7)
    n = 1000
    x = np.zeros(n)
    i = 0
    while i < n:
        L = int(rng.integers(20, 150))
        x[i:i + L] = rng.choice([0.0, 0.9], p=[.55, .45])
        i += L
    c = cfg()
    seg = StreamingSegmenter(c)
    locked_history = []
    for a, b in [(i, min(i + 23, n)) for i in range(0, n, 23)]:
        seg.push(x[a:b])
        locked_history.append(pairlist(seg.locked_intervals))
    final = pairlist(seg.finish())
    # 每次快照都是最终结果的前缀，且单调增长（不回退、不改写）
    prev_len = 0
    prev = []
    for snap in locked_history:
        assert final[:len(snap)] == snap
        assert len(snap) >= prev_len
        assert snap[:len(prev)] == prev
        prev, prev_len = snap, len(snap)
    assert final[:len(locked_history[-1])] == locked_history[-1]


def test_empty_and_single_sample():
    c = cfg()
    seg = StreamingSegmenter(c)
    seg.push(np.zeros(0))
    assert seg.finish() == []
    seg2 = StreamingSegmenter(c)
    seg2.push(np.zeros(1))  # 单个静音样本，流尾 S
    assert seg2.finish() == []
    seg3 = StreamingSegmenter(c)
    seg3.push(np.ones(1))  # 单个高样本，edge keep
    assert pairlist(seg3.finish()) == [(0, 1)]


def test_padding_clamped_at_both_edges():
    x = np.zeros(200)
    x[0:60] = 0.5       # 流首语音
    x[140:200] = 0.5    # 流尾语音（到流末）
    c = cfg(pad_before=30, pad_after=30, min_silence=50)
    seg = StreamingSegmenter(c)
    seg.push(x)
    ivs = pairlist(seg.finish())
    assert ivs == [(0, 90), (110, 200)]  # 两端都被 clamp


def test_push_after_finish_rejected():
    c = cfg()
    seg = StreamingSegmenter(c)
    seg.push(np.zeros(10))
    seg.finish()
    with pytest.raises(RuntimeError):
        seg.push(np.zeros(10))
