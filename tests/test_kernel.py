"""时间内核测试：全部期望值手工推导（例如 1/25→1/150 的因子为 6）。"""
from __future__ import annotations

import numpy as np
import pytest

from mediaconcat import kernel
from mediaconcat.kernel import TimebaseSelectionError


def test_scale_factor_known_values():
    # 25fps ticks(1/25s) -> 1/150s：1 tick = 6 ticks
    assert kernel.scale_factor((1, 25), (1, 150)) == (150, 25)
    assert kernel.reduce_factor((150, 25)) == (6, 1)
    # 30fps -> 1/150：1 tick = 5 ticks
    assert kernel.reduce_factor(kernel.scale_factor((1, 30), (1, 150))) == (5, 1)
    # 48k 音频包 1024 PCM = 1024 ticks @1/48000
    assert kernel.reduce_factor(kernel.scale_factor((1, 48000), (1, 48000))) == (1, 1)


def test_convert_ticks_exact_and_remainder():
    out, rem = kernel.convert_ticks(7, (1, 25), (1, 150))
    assert (out, rem) == (42, 0)
    # 有损方向：1/150 -> 1/25 每 tick 缩放 25/150=1/6
    out, rem = kernel.convert_ticks(1, (1, 150), (1, 25))
    assert out == 0 and rem == 25  # 不能整除，余数非零
    assert kernel.conversion_is_lossless((1, 25), (1, 150)) is True
    assert kernel.conversion_is_lossless((1, 150), (1, 25)) is False


def test_convert_series_matches_scalar():
    ticks = np.arange(0, 10, dtype=np.int64)
    out, rem = kernel.convert_series(ticks, (1, 25), (1, 150))
    assert out.tolist() == [6 * i for i in range(10)]
    assert rem.sum() == 0


def test_seconds_roundtrip():
    assert kernel.seconds_to_ticks(0.2, (1, 25)) == 5
    assert kernel.ticks_to_seconds(15360, (1, 48000)) == pytest.approx(0.32)


def test_lcm_known():
    assert kernel.lcm(25, 30) == 150
    assert kernel.lcm_many([25, 30, 48000]) == 48000  # 48000 已被 25/30 整除？验证
    # 48000 / 25 = 1920 整除，48000/30=1600 整除 ⇒ lcm=48000
    assert kernel.lcm_many([12800]) == 12800


def test_choose_output_timebase_mp4_lcm():
    tb = kernel.choose_output_timebase([(1, 25), (1, 30)], "mp4")
    assert tb == (1, 150)
    tb = kernel.choose_output_timebase([(1, 25), (1, 30), (1, 48000)], "mp4")
    assert tb == (1, 48000)


def test_choose_output_timebase_mpegts_divisibility():
    assert kernel.choose_output_timebase([(1, 25), (1, 30)], "mpegts") == (1, 90000)
    with pytest.raises(TimebaseSelectionError) as exc:
        kernel.choose_output_timebase([(1, 12800)], "mpegts")
    assert exc.value.reason == "mpegts_clock_not_divisible"
    assert exc.value.evidence["bad_source_denominators"] == [12800]


def test_choose_output_timebase_overflow():
    # 两个大素数分母 → LCM 溢出 uint32
    with pytest.raises(TimebaseSelectionError) as exc:
        kernel.choose_output_timebase([(1, 90001), (1, 90007)], "mp4")
    assert exc.value.reason == "timescale_overflow"


def test_rebase_preserves_pts_dts_offset():
    # 解码序 DTS=[0,1,2]，呈现序 PTS=[0,2,1]（一个 B 帧重排形状）
    dts = np.array([0, 1, 2], dtype=np.int64)
    pts = np.array([0, 2, 1], dtype=np.int64)
    dur = np.array([1, 1, 1], dtype=np.int64)
    o_dts, o_pts, o_dur, rem = kernel.rebase_series(
        dts, pts, dur, (1, 25), (1, 150), out_origin=60
    )
    assert o_dts.tolist() == [60, 66, 72]
    assert o_pts.tolist() == [60, 72, 66]
    # 逐样本偏移保持
    assert (o_pts - o_dts).tolist() == (6 * (pts - dts)).tolist()
    assert rem.sum() == 0


def test_monotonic_guard_detects_negative_and_regression():
    assert kernel.assert_monotonic_nonnegative(np.array([0, 1, 2])) is None
    assert kernel.assert_monotonic_nonnegative(np.array([-1, 0])) == 0
    assert kernel.assert_monotonic_nonnegative(np.array([0, 5, 3])) == 2


def test_priming_drop_aac_2112():
    packets = np.full(8, 1024, dtype=np.int64)
    contributions, roles = kernel.priming_drop(2112, packets)
    # 2112 = 2*1024 + 64：前两包全弃，第三包只留 960
    assert contributions.tolist() == [0, 0, 960, 1024, 1024, 1024, 1024, 1024]
    assert contributions.sum() == 8 * 1024 - 2112  # 可听样本守恒
    assert roles.tolist() == [1, 1, 1, 0, 0, 0, 0, 0]


def test_priming_drop_zero_is_identity():
    packets = np.full(3, 1024, dtype=np.int64)
    contributions, roles = kernel.priming_drop(0, packets)
    assert contributions.tolist() == [1024, 1024, 1024]
    assert roles.sum() == 0


def test_tail_padding_count():
    # 视频 0.32s@48k=15360，音频到 12288，包长 1024 → gap=3072 → 3 包
    assert kernel.tail_padding_count(15360, 12288, 1024) == 3
    assert kernel.tail_padding_count(12288, 12288, 1024) == 0
    # 非整倍数向上取整
    assert kernel.tail_padding_count(13300, 12288, 1024) == 1


def test_reference_closure_open_gop():
    refs = {5: [], 6: [5, 4], 7: [6]}
    # 无停止点：完整递归，帧4 被拉入
    closed = kernel.reference_closure({5, 6, 7, 8, 9}, refs)
    assert closed == {4, 5, 6, 7, 8, 9}


def test_reference_closure_stops_at_restart_point():
    # 链：0←1←2←5←6。把帧5声明为停止点时停在 {5,6}，否则回溯到帧0
    refs = {0: [], 1: [0], 2: [1], 5: [2], 6: [5]}
    closed = kernel.reference_closure({6}, refs, stop_at={5})
    assert closed == {5, 6}
    assert kernel.reference_closure({6}, refs) == {0, 1, 2, 5, 6}


def test_reference_closure_does_not_stop_at_open_gop_keyframe():
    # 帧5 带 keyframe 标志但引用更早帧4 → 不是合法停止点（调用方不会放入 stop_at）
    refs = {4: [], 5: [4], 6: [5]}
    assert kernel.reference_closure({6}, refs, stop_at=set()) == {4, 5, 6}
