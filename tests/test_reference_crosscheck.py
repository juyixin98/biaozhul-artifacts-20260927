"""被测内核 vs 独立参考实现（app.reference，逐样本循环）的随机交叉核对。

参考答案不来自被测内核：reference_segment 是另写的状态机实现，其正确性
另由 test_kernel_hand_verified.py 中手算期望锁定。这里用随机信号扩大覆盖面，
并交叉比较多种配置（padding/merge_gap/edge/min_speech/min_silence）。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.kernel import SegmentConfig, StreamingSegmenter
from app.reference import reference_segment


def random_signal(rng, n):
    """生成具有真实 run 结构的信号：恒定电平块 + 边界精确踩阈值的样本。"""
    x = np.zeros(n)
    i = 0
    palette = [0.0, 0.009, 0.03, 0.05, 0.079, 0.08, 0.1, 0.9]
    while i < n:
        L = int(rng.integers(1, 120))
        x[i:i + L] = palette[int(rng.integers(0, len(palette)))]
        i += L
    # 随机塞若干精确阈值样本，专门卡 >= / < 的边界
    for _ in range(n // 20):
        p = int(rng.integers(0, n))
        x[p] = rng.choice([0.03, 0.08])
    return x


def random_config(rng):
    return SegmentConfig(
        sample_rate=1000,
        enter_threshold=0.03, exit_threshold=0.08,
        min_speech=int(rng.integers(0, 90)),
        min_silence=int(rng.integers(0, 130)),
        pad_before=int(rng.integers(0, 35)),
        pad_after=int(rng.integers(0, 35)),
        merge_gap=int(rng.integers(0, 12)),
        edge_keep=bool(rng.integers(0, 2)))


@pytest.mark.parametrize("seed", range(120))
def test_kernel_matches_independent_reference(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(0, 700))
    x = random_signal(rng, n)
    c = random_config(rng)

    seg = StreamingSegmenter(c)
    seg.push(x)
    got = [(iv.start, iv.end) for iv in seg.finish()]
    ref = [(iv.start, iv.end)
           for iv in reference_segment(x.tolist(), c, edge=True)]
    assert got == ref, (seed, c)


@pytest.mark.parametrize("seed", range(40))
def test_reference_agrees_across_chunkings_vs_whole(seed):
    """切块喂入内核，必须与参考在整段信号上的结论一致。"""
    rng = np.random.default_rng(5000 + seed)
    n = int(rng.integers(50, 600))
    x = random_signal(rng, n)
    c = random_config(rng)
    ref = [(iv.start, iv.end)
           for iv in reference_segment(x.tolist(), c, edge=True)]
    for w in (1, 2, 5, 29, 97, max(1, n // 4)):
        seg = StreamingSegmenter(c)
        for a in range(0, n, w):
            seg.push(x[a:min(a + w, n)])
        got = [(iv.start, iv.end) for iv in seg.finish()]
        assert got == ref, (seed, w)


def test_reference_tracks_state_per_sample():
    """锁定参考实现的逐样本语义：中间带保持，两侧阈值边界方向相反。"""
    c = SegmentConfig(1000, 0.03, 0.08, 1, 1, 0, 0, 0, True)
    x = [0.0, 0.08, 0.05, 0.03, 0.029]
    # S, H(>=exit), H(保持), H(==enter 保持), S(<enter)
    # 语音区域 [1,4)
    assert [(iv.start, iv.end)
            for iv in reference_segment(x, c)] == [(1, 4)]
