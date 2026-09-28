"""手工核验区间：每个用例的期望区间都是人手逐步推演并写死的。

覆盖需求点名的四类信号：
  1. 阈值附近脉冲（含中间带滞回、短噪声不割裂长静音）
  2. 跨块长静音（分段 push 与一次性 push 结果一致）
  3. 全静音
  4. 末尾未完成段（edge_keep 两种取值行为相反，均有断言）

同时断言独立参考实现（app.reference，逐样本循环）给出相同区间，
即期望答案不是由被测内核自己产生的。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.kernel import (Interval, SegmentConfig, StreamingSegmenter,
                        build_intervals)
from app.reference import reference_segment


def cfg(**kw) -> SegmentConfig:
    base = dict(sample_rate=1000, enter_threshold=0.03,
                exit_threshold=0.08, min_speech=50, min_silence=100,
                pad_before=10, pad_after=20, merge_gap=0, edge_keep=True)
    base.update(kw)
    return SegmentConfig(**base)


def run_kernel(x, c):
    seg = StreamingSegmenter(c)
    seg.push(np.asarray(x, dtype=np.float64))
    return seg.finish()


def pairs(ivs):
    return [(iv.start, iv.end) for iv in ivs]


# 三个实现必须在每一个手算用例上一致：流式内核、整体 RLE 内核、独立参考
def assert_all_agree(x, c, expected_pairs):
    expected = [Interval(a, b) for a, b in expected_pairs]
    k_stream = run_kernel(x, c)
    whole_seg = StreamingSegmenter(c)
    whole_seg.push(np.asarray(x, dtype=np.float64))
    k_whole = build_intervals(whole_seg.runs, len(x), c, edge=True)
    ref = reference_segment(list(x), c, edge=True)
    assert pairs(k_stream) == expected_pairs
    assert pairs(k_whole) == expected_pairs
    assert pairs(ref) == expected_pairs
    return k_stream, ref


def _whole_runs(x, c):
    seg = StreamingSegmenter(c)
    seg.push(np.asarray(x, dtype=np.float64))
    return seg.runs


# ---------------------------------------------------------------------------
# 1) 阈值附近脉冲：中间带滞回 + 短噪声不割裂长静音
# ---------------------------------------------------------------------------

def test_near_threshold_pulses_hand_verified():
    x = np.zeros(1200)
    x[0:200] = 0.5            # H 200
    x[300:330] = 0.05         # 中间带脉冲（0.03 <= 0.05 < 0.08），30 < 50
    x[500:580] = 0.5          # H 80
    x[700:740] = 0.05         # 中间带脉冲，40 < 50
    x[900:1000] = 0.5         # H 100
    c = cfg()

    # 逐步推演（状态机 run，长度单位=样本）:
    #   H[0,200)  S[200,300)=100  S[300,330)=30（中间带保持 S！）
    #   S[330,500)=170  H[500,580)=80  S[580,700)=120
    #   S[700,740)=40（仍保持 S）  S[740,900)=160
    #   H[900,1000)=100  S[1000,1200)=200
    # 关键：0.05 不 >= exit 0.08，状态机根本不翻转，"短噪声"由静音吞并。
    seg = StreamingSegmenter(c)
    seg.push(x)
    raw = [(r.start, r.end, r.raw) for r in seg.runs]
    assert raw == [
        (0, 200, "H"), (200, 500, "S"),
        (500, 580, "H"), (580, 900, "S"),
        (900, 1000, "H"), (1000, 1200, "S"),
    ]

    # pad 10/20 后:
    #   [max(0,0-10)=0, 200+20=220)
    #   [500-10=490, 580+20=600)
    #   [900-10=890, 1000+20=1020)
    # 间隔 270/290 远大于 merge_gap=0，不合并且不越界。
    k, ref = assert_all_agree(x, c, [(0, 220), (490, 600), (890, 1020)])


def test_exact_threshold_levels_hysteresis():
    """恰好等于阈值的样本：exit 用 >=，enter 用 <。"""
    x = np.zeros(100)
    x[20:40] = 0.08   # == exit：S->H
    x[60:80] = 0.03   # == enter：在 H 侧时保持 H（不翻转！）
    c = cfg()
    seg = StreamingSegmenter(c)
    seg.push(x)
    raw = [(r.start, r.end, r.raw) for r in seg.runs]
    # S[0,20) H[20,40) S[40,60)；60..80 的 0.03 >= enter -> 继续 S
    assert raw == [(0, 20, "S"), (20, 40, "H"), (40, 100, "S")]

    # 反过来：先拉高再回到 0.03，必须保持 H
    y = np.zeros(200)
    y[10:30] = 0.08   # 进入 H
    y[30:60] = 0.03   # 中间带下沿，保持 H
    y[60:75] = 0.029  # < enter：翻 S
    seg2 = StreamingSegmenter(cfg(min_silence=40))
    seg2.push(y)
    raw2 = [(r.start, r.end, r.raw) for r in seg2.runs]
    # 首部 S[0,10) 短；H[10,60)=50 保持；尾部 S[60,200)=140 确认静音
    assert raw2 == [(0, 10, "S"), (10, 60, "H"), (60, 200, "S")]
    # H run 长 50 == min_speech 50 -> speech；首 S 短被并入，区间 [0, 80)
    assert pairs(seg2.finish()) == [(0, 80)]


def test_short_noise_between_long_silences():
    """真的翻到 H 的短脉冲（高电平但 < min_speech），被两侧静音吞并。"""
    x = np.zeros(600)
    x[200:230] = 0.9    # 30 样本 H 短噪声
    x[400:440] = 0.9    # 40 样本 H 短噪声
    c = cfg()
    # runs: S200 H30 S170 H40 S160；两个 H 都 < min_speech -> 全 silence
    assert_all_agree(x, c, [])


def test_short_silence_gap_bridges_speech():
    """两段语音间的短静缝（< min_silence）不切断，且判定理由与短噪声不同。"""
    x = np.zeros(500)
    x[0:120] = 0.5
    x[200:320] = 0.5    # 中间 S[120,200) 长 80 < min_silence 100
    c = cfg()
    # runs: H120 S80 H120 S180；S80 标 speech -> 连续语音区域 [0,320)，
    # 尾部 S180>=100 确认静音，pad_after 后 [0,340)
    assert_all_agree(x, c, [(0, 340)])


def test_merge_gap_joins_padded_intervals():
    """merge_gap 显式把被窄静音分开的区间合并。"""
    x = np.zeros(500)
    x[0:60] = 0.5
    x[140:200] = 0.5   # S[60,140)=80 < min_silence100，本来就连通
    c_gap0 = cfg()
    assert_all_agree(x, c_gap0, [(0, 220)])

    # 改成 min_silence 更短也仍连通；真正测 merge_gap：把间隙做成
    # 已确认静音但 pad 后几乎接触
    y = np.zeros(700)
    y[0:80] = 0.5
    y[200:280] = 0.5    # S[80,200)=120 确认静音
    c2 = cfg(min_silence=100, pad_before=30, pad_after=30, merge_gap=25)
    # 区间1 [0,110)，区间2 [170,310)；间隔 60 <= 25? 否 -> 不合并
    assert_all_agree(y, c2, [(0, 110), (170, 310)])
    c3 = cfg(min_silence=100, pad_before=30, pad_after=30, merge_gap=60)
    # 间隔 60 <= merge_gap 60 -> 合并为 [0,310)
    assert_all_agree(y, c3, [(0, 310)])


# ---------------------------------------------------------------------------
# 2) 跨块长静音：区间与切块方式无关（在不变性测试里做穷举，这里手算一例）
# ---------------------------------------------------------------------------

def test_cross_chunk_long_silence_hand_verified():
    x = np.zeros(1200)
    x[0:200] = 0.5
    x[600:800] = 0.5     # S[200,600)=400 确认静音
    c = cfg()
    expected = [(0, 220), (590, 820)]

    # 手工选择的恶意切点：正切在静音中间、run 边界上、块长 1
    cuts = {
        "whole": [(0, 1200)],
        "at_silence_mid": [(0, 400), (400, 1200)],
        "at_run_boundary": [(0, 200), (200, 600), (600, 800), (800, 1200)],
        "odd_3": [(i, min(i + 3, 1200)) for i in range(0, 1200, 3)],
        "prime_17": [(i, min(i + 17, 1200)) for i in range(0, 1200, 17)],
        "byte_by_byte": [(i, i + 1) for i in range(1200)],
    }
    for name, cut in cuts.items():
        seg = StreamingSegmenter(c)
        for a, b in cut:
            seg.push(x[a:b])
        assert pairs(seg.finish()) == expected, name

    # 锁定行为：吃到静音第 lock_length 个样本前，第二区间不得提交。
    # lock_length = max(100, 10+20+0+1)=100。
    seg = StreamingSegmenter(c)
    seg.push(x[:300])   # 静音只观测到 100 个样本（200..300）
    assert pairs(seg.locked_intervals) == [(0, 220)]
    seg.push(x[300:599])  # 仍在静音 [200,600) 内
    assert pairs(seg.locked_intervals) == [(0, 220)]
    seg.push(x[599:600])  # 静音长度 400，第二区间仍未出现（语音没来）
    seg.push(x[600:])     # 喂完第二段语音与其后静音
    assert pairs(seg.finish()) == expected


# ---------------------------------------------------------------------------
# 3) 全静音：空区间，且样本守恒 stats 中 kept=0
# ---------------------------------------------------------------------------

def test_all_silence():
    x = np.zeros(500)
    c = cfg()
    assert_all_agree(x, c, [])
    # 全静音但电平在 enter 之上、exit 之下（中间带）也应全 S
    y = np.full(500, 0.05)
    assert_all_agree(y, c, [])
    # 空输入
    assert reference_segment([], c) == []
    seg = StreamingSegmenter(c)
    seg.push(np.zeros(0))
    assert seg.finish() == []


def test_all_loud():
    x = np.full(500, 0.9)
    c = cfg()
    # 单个 H run 与流首尾相接；pad 两端 clamp -> [0,500)
    assert_all_agree(x, c, [(0, 500)])


# ---------------------------------------------------------------------------
# 4) 末尾未完成段：edge_keep 两种取值
# ---------------------------------------------------------------------------

def test_tail_unfinished_edge_keep_true():
    x = np.zeros(750)
    x[400:750] = 0.5   # S400 后接 H350，无收尾静音
    c = cfg(edge_keep=True)
    # 尾部 H 与流尾相接，即使 350>=50 本就保留；区间 [390,750)
    assert_all_agree(x, c, [(390, 750)])


def test_tail_unfinished_short_kept_only_by_edge_rule():
    """尾部 H 短于 min_speech 且 H 就是流尾 run：edge 保留/严格丢弃。"""
    x = np.zeros(430)
    x[400:430] = 0.5   # S400 确认静音，尾部 H30 < min_speech 50，直接结束
    c_t = cfg(edge_keep=True)
    c_f = cfg(edge_keep=False)
    assert_all_agree(x, c_t, [(390, 430)])     # pad_before 10；流尾无 pad_after 空间
    assert_all_agree(x, c_f, [])               # 严格规则：丢弃尾部短段


def test_head_unfinished_short_kept_only_by_edge_rule():
    """流首 H 短于 min_speech：edge_keep 对称处理。"""
    x = np.zeros(400)
    x[0:30] = 0.5
    c_t = cfg(edge_keep=True)
    c_f = cfg(edge_keep=False)
    assert_all_agree(x, c_t, [(0, 50)])       # pad_after 20
    assert_all_agree(x, c_f, [])


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------

def test_config_rejects_inverted_thresholds():
    with pytest.raises(ValueError, match="enter_threshold"):
        SegmentConfig(sample_rate=1000, enter_threshold=0.1,
                      exit_threshold=0.05, min_speech=1, min_silence=1,
                      pad_before=0, pad_after=0, merge_gap=0, edge_keep=True)
    with pytest.raises(ValueError):
        cfg(enter_threshold=0.08, exit_threshold=0.08)
