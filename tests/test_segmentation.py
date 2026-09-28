"""信号内核测试。

四类问题都有**具体断言**（不是“能调用”）：
1. 阈值附近脉冲：精确区间 + 短噪声不割裂；
2. 跨块长静音：多个互切块大小（含逐样本）结果完全一致（切块不变性）；
3. 全静音（含短于最小静音的）：空结果；
4. 末尾未完成段：三种尾态都保留到音频末尾。
另含：与独立标量 oracle 的随机一致性、边界等号、迟滞带、合并规则、
非有限样本 -> COMPUTATION_FAILED、参数非法 -> INVALID_ARGUMENT。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.errors import SegmentError
from app.segmentation import (
    LABEL_LOUD,
    LABEL_LOW,
    LABEL_MID,
    Params,
    SilenceSegmenter,
    finalize_intervals,
    segment_samples,
    validate_intervals,
)
from conftest import (
    oracle_finalize,
    oracle_segment_raw,
    signal_long_silence,
    signal_threshold_pulse,
    signal_trailing_cases,
)

SR = 1000
P = dict(
    sample_rate=SR, enter_threshold=0.02, exit_threshold=0.05,
    min_silence=300, min_activity=100, pad=50, merge_gap=120,
)


def _params(**over):
    return Params(**{**P, **over})


def _feed(sig, params, chunk_size=None):
    seg = SilenceSegmenter(params)
    if chunk_size is None:
        seg.process(np.asarray(sig, dtype=np.float64))
    else:
        i = 0
        for c in chunk_size:
            seg.process(np.asarray(sig[i : i + c], dtype=np.float64))
            i += c
        assert i == len(sig)
    return seg.finish()


# ---------------------------------------------------------------------------
# 1. 阈值附近脉冲（手工核验，数字写死）
# ---------------------------------------------------------------------------


class TestThresholdPulse:
    def test_exact_intervals(self):
        sig = signal_threshold_pulse()
        raw = _feed(sig, _params())
        assert raw == [(205, 330)]  # 手工：MID 后 LOUD 起点 205，LOW 锚点 330
        iv = finalize_intervals(raw, sig.size, P["pad"], P["merge_gap"])
        assert iv == [(155, 380)]

    def test_short_blip_does_not_split_nor_emit(self):
        # 尾部 10 样本 LOUD（330+300=630 起，长度 10 < min_activity=100），
        # 随后 430 LOW。它必须既不产生第二区间，也不影响第一区间边界。
        sig = signal_threshold_pulse()
        raw = _feed(sig, _params())
        assert len(raw) == 1
        seg = SilenceSegmenter(_params())
        seg.process(sig[:641])  # 脉冲(630..640)后再含 1 个 LOW
        assert seg.state == "silent"  # 脉冲后状态仍回静音
        # 脉冲的 loud 计数被其后的 LOW 冲掉：snapshot 中无进行中 loud run
        assert seg._loud_len == 0
        # 且脉冲起点不被保留（没有挂起的新段）
        assert seg._seg_start is None

    def test_mid_band_resets_entry_countdown(self):
        # active 下 LOW 累计了 299（差 1 进入静音），来一个 MID，再给
        # 300 LOW：进入时刻必须以“MID 之后”的 LOW 重新累计。
        sig = (
            [0.5] * 100          # 0..100 确认活动
            + [0.0] * 299        # 100..399
            + [0.03] * 1         # 399 MID，冲掉计数
            + [0.0] * 300        # 400..700
        )
        raw = _feed(sig, _params())
        assert raw == [(0, 400)]  # 锚点是 MID 后的 LOW 起点 400


# ---------------------------------------------------------------------------
# 2. 跨块长静音 + 切块不变性
# ---------------------------------------------------------------------------


class TestChunkInvariance:
    @pytest.mark.parametrize(
        "chunks",
        [
            None,
            [800],
            [1] * 800,                 # 逐样本
            [7, 13, 1, 99, 37, 53, 200, 9, 199, 101, 81],
            [333, 234, 233],
            [256, 256, 256, 32],
            [2, 4, 8, 16, 32, 64, 128, 256, 290],
        ],
    )
    def test_same_regardless_of_chunking(self, chunks):
        sig = signal_long_silence()
        raw = _feed(sig, _params(), chunks)
        iv = finalize_intervals(raw, sig.size, P["pad"], P["merge_gap"])
        # 手工：两段声音 0..100、700..800，中间 600 静音不合并
        assert raw == [(0, 100), (700, 800)]
        assert iv == [(0, 150), (650, 800)]

    def test_threshold_completed_exactly_on_next_chunk(self):
        # 回归：连续 LOW 在块 A 累计 299、块 B 再补 1（恰好 300）。
        # 必须在第 400 个样本（LOW 锚点 100）确认进入静音，而不是拖到 finish。
        sig = [0.5] * 100 + [0.0] * 300 + [0.5] * 150 + [0.0] * 50
        params = _params()
        seg = SilenceSegmenter(params)
        seg.process(np.asarray(sig[:399], dtype=np.float64))
        assert seg.state == "active"
        seg.process(np.asarray(sig[399:400], dtype=np.float64))  # 仅补 1 个 LOW
        # 恰在第 400 个样本（LOW 锚点 100）确认进入静音，不拖到 finish。
        assert seg.state == "silent"
        seg.process(np.asarray(sig[400:], dtype=np.float64))
        assert seg.finish() == [(0, 100), (400, 600)]

    def test_carry_state_spans_chunks(self):
        # 一个 LOW 游程被切在多块中间，计数必须累加而非重置：
        # active 后 150 LOW 在块 A、150 LOW 在块 B（合计 300 触发进入静音）。
        params = _params()
        seg = SilenceSegmenter(params)
        seg.process(np.array([0.5] * 100, dtype=np.float64))
        seg.process(np.array([0.0] * 150, dtype=np.float64))
        snap = seg.snapshot()
        # 未终止的 LOW 游程跨块延续，且其长度已计入“进入静音”计数器：
        # 状态仍 active，连续 LOW 已累计 150（差 150）。
        assert snap["state"] == "active"
        assert snap["carry"] == {"label": "low", "length": 150}
        assert snap["pending_low"] == {"start": 100, "length": 150}
        seg.process(np.array([0.0] * 150, dtype=np.float64))
        assert seg.state == "silent"  # 跨块续满 300 -> 进入
        raw = seg.finish()
        assert raw == [(0, 100)]


# ---------------------------------------------------------------------------
# 3. 全静音
# ---------------------------------------------------------------------------


class TestAllSilence:
    def test_all_silence_empty(self):
        sig = np.zeros(1000)
        assert _feed(sig, _params()) == []
        res = segment_samples(sig, _params())
        assert res.intervals == []
        assert res.raw_ranges == []

    def test_silence_shorter_than_min_silence_still_empty(self):
        # 250 LOW < min_silence 300：从没有活动，自然为空（不能误造尾段）。
        assert _feed(np.zeros(250), _params()) == []

    def test_longer_than_min_silence_empty(self):
        assert _feed(np.zeros(5000), _params()) == []


# ---------------------------------------------------------------------------
# 4. 末尾未完成段
# ---------------------------------------------------------------------------


class TestTrailingOpen:
    @pytest.mark.parametrize(
        "key,expected_raw,expected_iv",
        [
            ("open_loud", (200, 300), (150, 300)),
            ("unconfirmed_low", (200, 400), (150, 400)),
            ("mid_tail", (200, 330), (150, 330)),
        ],
    )
    def test_trailing_kept_to_end(self, key, expected_raw, expected_iv):
        sig = signal_trailing_cases()[key]
        raw = _feed(sig, _params())
        assert raw == [expected_raw]
        iv = finalize_intervals(raw, sig.size, P["pad"], P["merge_gap"])
        assert iv == [expected_iv]

    def test_trailing_after_silence_emits_nothing(self):
        # 活动已被 300 LOW 闭合后，剩下的尾巴全是静音 -> 没有尾段。
        sig = np.array([0.0] * 200 + [0.5] * 100 + [0.0] * 400)
        assert _feed(sig, _params()) == [(200, 300)]


# ---------------------------------------------------------------------------
# 边界等号与迟滞
# ---------------------------------------------------------------------------


class TestThresholdBoundaries:
    def test_equality_uses_closed_bands(self):
        # enter=exit=0.05：|x|==0.05 必须算 LOUD（>=），|x| 恰小于才算 LOW。
        sig = np.array([0.0] * 10 + [0.05] * 20 + [0.0] * 10)
        p = _params(
            enter_threshold=0.05, exit_threshold=0.05,
            min_silence=10, min_activity=10, pad=0, merge_gap=0,
            sample_rate=100,
        )
        assert _feed(sig, p) == [(10, 30)]

    def test_hysteresis_mid_neither_enters_nor_exits(self):
        # silent 下：99 LOUD（差 1 退出）+ 1 MID + 99 LOUD -> 不退出。
        sig = np.array([0.0] * 50 + [0.1] * 99 + [0.03] + [0.1] * 99)
        assert _feed(sig, _params()) == []


class TestMergeRule:
    @pytest.mark.parametrize(
        "merge_gap,expected",
        [(30, [(0, 50)]), (29, [(0, 10), (40, 50)])],
    )
    def test_gap_threshold_is_inclusive(self, merge_gap, expected):
        # sr=100：10 LOUD + 30 LOW(=300ms 静音) + 10 LOUD，无保留。
        sig = np.array([0.5] * 10 + [0.0] * 30 + [0.5] * 10)
        p = _params(
            sample_rate=100, min_silence=30, min_activity=5,
            pad=0, merge_gap=merge_gap,
        )
        raw = _feed(sig, p)
        assert raw == [(0, 10), (40, 50)]
        assert finalize_intervals(raw, sig.size, 0, merge_gap) == expected

    def test_padding_shrinks_gap_then_merge(self):
        # 两区间 [0,10) [40,50)，pad=20 -> [0,30) [20,50) 已重叠 -> 合并。
        raw = [(0, 10), (40, 50)]
        assert finalize_intervals(raw, 50, pad=20, merge_gap=0) == [(0, 50)]

    def test_padding_clamped_to_audio_domain(self):
        assert finalize_intervals([(5, 15)], 20, pad=100, merge_gap=0) == [(0, 20)]


# ---------------------------------------------------------------------------
# 与独立 oracle 的随机一致性（参考答案不来自被测代码）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_matches_independent_oracle(seed):
    rng = np.random.default_rng(seed)
    n = int(rng.integers(300, 2000))
    # 从 {0, .01, .03, .08} 里抽，保证三带与边界附近都有覆盖
    vals = np.array([0.0, 0.01, 0.03, 0.08])
    sig = rng.choice(vals, size=n)
    min_sil = int(rng.integers(1, 80))
    min_act = int(rng.integers(1, 40))
    pad = int(rng.integers(0, 30))
    gap = int(rng.integers(0, 60))
    p = _params(
        min_silence=min_sil, min_activity=min_act, pad=pad, merge_gap=gap
    )
    got_raw = _feed(sig, p)
    want_raw = oracle_segment_raw(
        sig, enter=p.enter_threshold, exit_=p.exit_threshold,
        min_silence=min_sil, min_activity=min_act,
    )
    assert got_raw == want_raw
    got_iv = finalize_intervals(got_raw, n, pad, gap)
    want_iv = oracle_finalize(want_raw, n, pad, gap)
    assert got_iv == want_iv
    # 切块不变性也在随机信号上验一遍
    cs = max(1, int(rng.integers(1, 37)))
    assert _feed(sig, p, [cs] * (n // cs) + ([n - cs * (n // cs)] if n % cs else [])) == want_raw


# ---------------------------------------------------------------------------
# 样本守恒 / 不变量（随机信号）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [11, 22])
def test_conservation_and_bounds(seed):
    rng = np.random.default_rng(seed)
    sig = rng.choice([0.0, 0.01, 0.1], size=1500)
    p = _params(pad=20, merge_gap=40)
    res = segment_samples(sig, p, chunk_size=127)
    iv = res.intervals
    validate_intervals(iv, sig.size)
    assert sum(e - s for s, e in iv) <= sig.size
    assert all(0 <= s < e <= sig.size for s, e in iv)
    assert all(b[0] <= a[1] for a, b in zip(iv, iv[1:])) is True  # 有序
    # 不重叠：后段起点 >= 前段终点
    assert all(nxt[0] >= prv[1] for prv, nxt in zip(iv, iv[1:]))


# ---------------------------------------------------------------------------
# 失败类别：COMPUTATION_FAILED / INVALID_ARGUMENT
# ---------------------------------------------------------------------------


class TestFailures:
    def test_nan_sample_is_computation_failure(self):
        sig = np.array([0.0, np.nan, 0.1], dtype=np.float64)
        seg = SilenceSegmenter(_params())
        with pytest.raises(SegmentError) as ei:
            seg.process(sig)
        assert ei.value.code == "COMPUTATION_FAILED"
        assert ei.value.category == "computation"
        assert ei.value.details["sample_index"] == 1

    def test_inf_sample_is_computation_failure(self):
        seg = SilenceSegmenter(_params())
        with pytest.raises(SegmentError) as ei:
            seg.process(np.array([np.inf], dtype=np.float64))
        assert ei.value.code == "COMPUTATION_FAILED"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"enter_threshold": -0.1},
            {"enter_threshold": 0.9, "exit_threshold": 0.1},  # 进入高于退出
            {"min_silence": -1},
            {"sample_rate": 0},
            {"pad": -3},
        ],
    )
    def test_bad_params_are_invalid_argument(self, kwargs):
        with pytest.raises(SegmentError) as ei:
            _params(**kwargs).validate()
        assert ei.value.code == "INVALID_ARGUMENT"
        assert ei.value.category == "input"

    def test_2d_input_rejected(self):
        seg = SilenceSegmenter(_params())
        with pytest.raises(SegmentError) as ei:
            seg.process(np.zeros((2, 2)))
        assert ei.value.code == "INVALID_ARGUMENT"

    def test_labels_directly(self):
        from app.segmentation import _classify

        labels = _classify(np.array([0.0, 0.019, 0.02, 0.049, 0.05]), 0.02, 0.05)
        assert list(labels) == [LABEL_LOW, LABEL_LOW, LABEL_MID, LABEL_MID, LABEL_LOUD]
