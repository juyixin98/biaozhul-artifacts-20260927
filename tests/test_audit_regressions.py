"""对抗性回归：针对独立代码审查发现的假阳性/边界缺陷。

这些用例直接构造 StreamProbe（不经规划器自身生成期望），断言“不可直拼
绝不被误判为可直拼”以及时间轴的精确实体结果。
"""
from __future__ import annotations

import pytest

from mediaconcat import kernel
from mediaconcat.models import (
    ClipProbe,
    FailureCode,
    FrameRecord,
    Severity,
    StreamProbe,
)
from mediaconcat.planner import plan_concat


def vframe(i, key=False, refs=None, dts=None, pts=None, dur=1):
    return FrameRecord(
        index=i, dts=i if dts is None else dts, pts=i if pts is None else pts,
        duration=dur, keyframe=key, references=refs or [],
    )


def aframe(i, pcm=1024, step=None, dur=None):
    step = pcm if step is None else step
    dur = pcm if dur is None else dur
    return FrameRecord(index=i, dts=i * step, pts=i * step, duration=dur,
                       keyframe=True, pcm_samples=pcm)


def vstream(frames, tb=(1, 25), **kw):
    return StreamProbe(codec="h264", codec_type="video", time_base=tb,
                       frames=frames, width=320, height=240, **kw)


def astream(frames, tb=(1, 48000), rate=48000, delay=0, **kw):
    return StreamProbe(codec="aac", codec_type="audio", time_base=tb,
                       frames=frames, sample_rate=rate, channels=2,
                       encoder_delay_samples=delay, **kw)


def clip(name, v=None, a=None, **cut):
    return ClipProbe(source=name, video=v, audio=a, **cut)


def err_codes(plan):
    return [f.code for f in plan.findings if f.severity == Severity.ERROR]


# ------------------------------------------------- S1：边界关键帧与 cut 解耦

def test_s1_second_clip_non_keyframe_without_explicit_cut_rejected():
    a = vstream([vframe(0, True), vframe(1, False, [0]), vframe(2, False, [1])])
    # 第二片段首帧非关键帧、且无 cut_in（真实分片极常见）
    b = vstream([vframe(0, False), vframe(1, False, [0]), vframe(2, False, [1])])
    plan = plan_concat([clip("a", a), clip("b", b)], "mp4", job_id="s1")
    assert plan.feasible is False
    assert FailureCode.NON_KEYFRAME_CUT in err_codes(plan)


def test_s1_clean_keyframe_boundary_concatenates():
    a = vstream([vframe(0, True), vframe(1, False, [0])])
    b = vstream([vframe(0, True, refs=[]), vframe(1, False, [0])])
    plan = plan_concat([clip("a", a), clip("b", b)], "mp4", job_id="s1ok")
    assert plan.feasible is True
    assert err_codes(plan) == []


@pytest.mark.parametrize("bad_refs", [
    [-1],     # 开放 GOP IDR：引用上一片段
    [0],      # 畸形自引用关键帧
    [1],      # 引用更晚帧（乱序）
])
def test_s1_keyframe_with_any_reference_is_not_clean_restart(bad_refs):
    head = vstream([vframe(0, True, refs=[]), vframe(1, False, [0])])
    tail = vstream([vframe(0, True, refs=bad_refs), vframe(1, False, [0])])
    plan = plan_concat([clip("head", head), clip("tail", tail)], "mp4", job_id="s1bad")
    assert plan.feasible is False
    assert err_codes(plan), "带任何引用的关键帧都不能作为拼接边界的干净重启点"


# ------------------------------------------------- S2：编码参数一致性

@pytest.mark.parametrize("field,va,vb", [
    ("pixel_format", "yuv420p", "yuv444p"),
    ("profile", "baseline", "high"),
    ("sample_aspect_ratio", "1/1", "4/3"),
])
def test_s2_video_param_mismatch_rejected(field, va, vb):
    a = vstream([vframe(0, True)], **{field: va})
    b = vstream([vframe(0, True)], **{field: vb})
    plan = plan_concat([clip("a", a), clip("b", b)], "mp4", job_id="s2")
    assert plan.feasible is False
    assert FailureCode.VIDEO_PARAM_MISMATCH in err_codes(plan)


def test_s2_extradata_mismatch_rejected():
    a = vstream([vframe(0, True)], extradata_id="sps-A")
    b = vstream([vframe(0, True)], extradata_id="sps-B")
    plan = plan_concat([clip("a", a), clip("b", b)], "mp4", job_id="s2x")
    assert FailureCode.VIDEO_PARAM_MISMATCH in err_codes(plan)


def test_s2_identical_params_pass():
    a = vstream([vframe(0, True)], pixel_format="yuv420p", extradata_id="same")
    b = vstream([vframe(0, True)], pixel_format="yuv420p", extradata_id="same")
    plan = plan_concat([clip("a", a), clip("b", b)], "mp4", job_id="s2ok")
    assert plan.feasible is True


# ---------------- H1：开放 GOP 参考链必须完整回溯到真正重启点

def test_h1_open_gop_chain_fully_retained_for_first_clip():
    # 0 IDR;1->0;2->1; 3 IDR; 4 refs=[3,2]; 5->4；从帧4起切
    frames = [vframe(0, True), vframe(1, False, [0]), vframe(2, False, [1]),
              vframe(3, True), vframe(4, False, [3, 2]), vframe(5, False, [4])]
    c = clip("c", vstream(frames), cut_in_sec=4 / 25)
    plan = plan_concat([c], "mp4", job_id="h1")
    assert plan.feasible is True
    seg = next(s for s in plan.segments if s.stream == "video")
    # 帧4 依赖帧2，帧2 又依赖 1→0；闭包必须拉到帧0，不能截断在帧2
    assert seg.retained_src_indices == [0, 1, 2, 3, 4, 5]


def test_h1_open_gop_chain_crossing_boundary_rejected():
    head = vstream([vframe(0, True), vframe(1, False, [0])])
    frames = [vframe(0, True), vframe(1, False, [0]), vframe(2, False, [1]),
              vframe(3, True), vframe(4, False, [3, 2]), vframe(5, False, [4])]
    tail = clip("tail", vstream(frames), cut_in_sec=4 / 25)
    plan = plan_concat([clip("head", head), tail], "mp4", job_id="h1b")
    assert plan.feasible is False
    assert FailureCode.NON_KEYFRAME_CUT in err_codes(plan)


# ---------------- H3：非 IDR 关键帧（引用更早）不是重启点

def test_h3_non_idr_keyframe_with_earlier_ref_is_not_restart_point():
    frames = [vframe(0, True), vframe(1, False, [0]),
              vframe(2, True, [1]),   # 带 keyframe 标志但引用更早 → CRA 风格
              vframe(3, False, [2])]
    c = clip("c", vstream(frames), cut_in_sec=3 / 25)
    plan = plan_concat([c], "mp4", job_id="h3")
    assert plan.feasible is True
    seg = next(s for s in plan.segments if s.stream == "video")
    assert seg.retained_src_indices == [0, 1, 2, 3]


# ------------------------------------------------- H4：重复 DTS 必须拒绝

def test_h4_duplicate_dts_detected():
    import numpy as np
    assert kernel.assert_monotonic_nonnegative(np.array([0, 1, 1, 3])) == 2


# ------------------------------------------------- H5：B帧裁剪按呈现序

def test_h5_bframe_cut_out_uses_presentation_order():
    # 4 帧小 GOP，解码序: I0(pts0) P3(pts3) B1(pts1) B2(pts2)
    frames = [
        vframe(0, True, refs=[], dts=0, pts=0),
        vframe(1, False, [0], dts=1, pts=3),
        vframe(2, False, [0, 1], dts=2, pts=1),
        vframe(3, False, [0, 1], dts=3, pts=2),
    ]
    # cut_out=2/25（排他）：内容为呈现 PTS∈[0,2) 即帧 pts0,pts1，
    # 解码序索引 {0(I),2(B1)}；pts1 的 B1 依赖 P(索引1)，闭包拉入 {1}；
    # 保留集合因此为 {0,1,2}，绝不包含呈现帧 pts3（解码序1之外）之外的 pts2 帧3。
    c = clip("c", vstream(frames), cut_out_sec=2 / 25)
    plan = plan_concat([c], "mp4", job_id="h5")
    assert plan.feasible is True
    seg = next(s for s in plan.segments if s.stream == "video")
    assert set(seg.retained_src_indices) == {0, 1, 2}
    content = {s.src_index for s in seg.samples if s.role == "content"}
    # 内容（呈现序 [0,2)）：解码帧0(I,pts0) 与帧2(B,pts1)；帧1(P,pts3) 是预滚参考
    assert content == {0, 2}
    roles = {s.src_index: s.role for s in seg.samples}
    assert roles[1] == "preroll_reference"


# ------------------------------------------------- M1：混合时基填充计数

def test_m1_padding_uses_unified_timebase_and_declared_duration_is_video():
    vframes = [vframe(i, key=(i == 0), refs=[] if i == 0 else [i - 1],
                      dts=i * 512, pts=i * 512, dur=512) for i in range(60)]
    c = clip("m1", vstream(vframes, tb=(1, 12800)), astream([aframe(i) for i in range(12)]))
    plan = plan_concat([c], "mp4", job_id="m1")
    assert plan.feasible is True
    assert plan.output_time_base == (1, 192000)
    aseg = next(s for s in plan.segments if s.stream == "audio")
    # gap = 2.4s(460800) - 0.256s(49152) = 411648；每包4096 → ceil=101
    assert aseg.pad_count == 101
    # 声明时长以视频内容为准（填充 overhang 不计）
    assert plan.output_duration_sec == pytest.approx(2.4, abs=1e-9)


# ------------------------------------------------- M2：裁剪后填充无缝

def test_m2_padding_after_cut_continues_without_gap():
    vframes = [vframe(i, key=(i == 0), refs=[] if i == 0 else [i - 1])
               for i in range(25)]
    apack = [aframe(i) for i in range(20)]
    c = clip("m2", vstream(vframes), astream(apack), cut_in_sec=0.32)
    plan = plan_concat([c], "mp4", job_id="m2")
    assert plan.feasible is True
    aseg = next(s for s in plan.segments if s.stream == "audio")
    real = [s for s in aseg.samples if s.src_index >= 0]
    pad = [s for s in aseg.samples if s.role == "silence_pad"]
    assert aseg.first_content_src_index == 15
    assert pad and pad[0].out_dts == real[-1].out_dts + real[-1].duration
    dts = [s.out_dts for s in aseg.samples]
    assert all(b > a for a, b in zip(dts, dts[1:]))


# ------------------------------------------------- M3/M4：priming 容器与多段

def test_m3_mkv_with_aac_priming_rejected():
    c = clip("m3", a=astream([aframe(i) for i in range(8)], delay=2112))
    plan = plan_concat([c], "mkv", job_id="m3")
    assert plan.feasible is False
    assert FailureCode.CONTAINER_CONSTRAINT in err_codes(plan)


def test_m4_subsequent_clip_with_priming_requires_transcode():
    c1 = clip("c1", a=astream([aframe(i) for i in range(8)], delay=2112))
    c2 = clip("c2", a=astream([aframe(i) for i in range(8)], delay=2112))
    plan = plan_concat([c1, c2], "mp4", job_id="m4")
    assert plan.feasible is False
    assert FailureCode.PRIMING_TRIM_UNSUPPORTED in err_codes(plan)


# ------------------------------------------------- M6：空流不崩溃

def test_m6_empty_video_is_explicit_finding_not_exception():
    c = clip("e", v=vstream([]))
    plan = plan_concat([c], "mp4", job_id="m6")
    assert plan.feasible is False
    assert err_codes(plan)  # 有明确类别，不抛 500


# ------------------------------------------------- L6：音频帧入 TS 非整数

def test_l6_aac44100_into_mpegts_rejected():
    pack = [FrameRecord(index=i, dts=i * 2048, pts=i * 2048, duration=2048,
                        keyframe=True, pcm_samples=1024) for i in range(4)]
    c = clip("l6", a=astream(pack, tb=(1, 90000), rate=44100))
    plan = plan_concat([c], "mpegts", job_id="l6")
    assert plan.feasible is False
    assert FailureCode.TIMEBASE_INCOMPATIBLE in err_codes(plan)


# ------------------------------------------------- L7：TS 负 PTS 预滚

def test_l7_mpegts_rejects_preroll_with_negative_pts():
    # 含 B 帧重排：从帧3(呈现PTS=3)起切时，预滚帧 B1/B2 的呈现早于入点 → 负 PTS。
    frames = [
        vframe(0, True, refs=[], dts=0, pts=0),
        vframe(1, False, [0], dts=1, pts=3),
        vframe(2, False, [0, 1], dts=2, pts=1),
        vframe(3, False, [0, 1], dts=3, pts=2),
    ]
    c_mp4 = clip("l7", vstream(frames), cut_in_sec=3 / 25)
    mp4 = plan_concat([c_mp4], "mp4", job_id="l7mp4")
    assert mp4.feasible is True
    c_ts = clip("l7ts", vstream(frames), cut_in_sec=3 / 25)
    ts = plan_concat([c_ts], "mpegts", job_id="l7ts")
    assert ts.feasible is False
    assert FailureCode.CONTAINER_CONSTRAINT in err_codes(ts)


# ------------------------------------------------- L8：cut_in=0 与 None 等价

def test_l8_cut_in_zero_equivalent_to_none_for_priming():
    c = clip("l8", a=astream([aframe(i) for i in range(8)], delay=2112),
             cut_in_sec=0.0)
    plan = plan_concat([c], "mp4", job_id="l8")
    # priming 仍应被识别并逐包扣减（不因 0.0 而误判 INVALID_RANGE）
    assert plan.feasible is True
    aseg = next(s for s in plan.segments if s.stream == "audio")
    contribs = [s.pcm_samples_contribution for s in aseg.samples]
    assert contribs == [0, 0, 960, 1024, 1024, 1024, 1024, 1024]


def test_ffprobe_like_incomplete_gop_structure_rejected_at_boundary():
    head = vstream([vframe(0, True, refs=[]), vframe(1, False, [0])],
                   reference_info_complete=True)
    # 即便第二片段以干净 IDR 起始，只要逐帧引用结构不可信（ffprobe 路径），
    # 就不能确认闭合 GOP → 拼接边界保守拒绝。
    tail = vstream([vframe(0, True, refs=[]), vframe(1, False, [0])],
                   reference_info_complete=False)
    plan = plan_concat([clip("h", head), clip("t", tail)], "mp4", job_id="gopunk")
    assert plan.feasible is False
    assert FailureCode.GOP_STRUCTURE_UNKNOWN in err_codes(plan)
    # 单片段无拼接边界，不因引用结构不可信而被拒绝
    solo = plan_concat([clip("solo", tail)], "mp4", job_id="gopunksolo")
    assert solo.feasible is True


# ------------------------------------------------- M5：时间戳溢出显式失败

def test_m5_huge_timestamp_overflow_is_explicit_finding():
    big = 10**19  # 超出 int64（上限 ~9.22e18）
    frames = [vframe(0, True, refs=[], dts=0, pts=0),
              vframe(1, False, [0], dts=big, pts=big)]
    c = clip("m5", v=vstream(frames, tb=(1, 90000)))
    plan = plan_concat([c], "mpegts", job_id="m5")
    assert plan.feasible is False
    assert FailureCode.TIMEBASE_CONVERSION_OVERFLOW in err_codes(plan)


def test_m5_extreme_lcm_raises_controlled_error_not_crash():
    from mediaconcat.kernel import TimebaseSelectionError, choose_output_timebase
    with pytest.raises(TimebaseSelectionError) as exc:
        choose_output_timebase([(1, 10**12), (1, 10**12 - 1)], "mp4")
    assert exc.value.reason == "timescale_overflow"


# ----------------------------------------------- 多轨 A/V 边界对齐（同步）

def _bb_av(n_audio):
    order = [(0, 0, True, []), (3, 1, False, [0]), (1, 2, False, [0, 3]),
             (2, 3, False, [0, 3]), (6, 4, False, [3]),
             (4, 5, False, [3, 6]), (5, 6, False, [3, 6])]
    vframes = [vframe(i, key=k, refs=r, dts=i, pts=pts)
               for i, (pts, d, k, r) in enumerate(order)]
    apack = [aframe(i) for i in range(n_audio)]
    return clip(
        "bb",
        vstream(vframes),
        astream(apack),
    )


def test_av_clip_audio_longer_than_video_breaks_boundary_sync_rejected():
    # 片段0 音频 20 包(0.427s) > 视频 0.28s；片段1 音频短。两媒体游标发散，
    # 下一片段 A/V 起点错位 → 直拼不可行（不能删音频包），必须转码。
    c1 = _bb_av(20)
    c1.source = "a"
    c2 = _bb_av(5)
    c2.source = "b"
    plan = plan_concat([c1, c2], "mp4", job_id="avsync")
    assert plan.feasible is False
    assert FailureCode.AV_DURATION_MISMATCH in err_codes(plan)


def test_av_equal_length_clips_concatenate_in_sync():
    # 视频 7 帧=0.28s=13440@48k；音频 14 包=14336 略长由边界判定，
    # 改为音频 13 包=13312（短 128 ticks，由部分静音帧精确填充到 13440）
    c1, c2 = _bb_av(13), _bb_av(13)
    c1.source, c2.source = "a", "b"
    plan = plan_concat([c1, c2], "mp4", job_id="avsync-ok")
    assert plan.feasible is True
    # 第二段两媒体起点必须相同（边界对齐，A/V 同步）
    v1 = next(s for s in plan.segments if s.stream == "video" and s.clip_index == 1)
    a1 = next(s for s in plan.segments if s.stream == "audio" and s.clip_index == 1)
    assert v1.samples[0].out_dts == a1.samples[0].out_dts == 13440
    # 音频最后一个（部分）填充帧的可听结束恰为视频边界 13440
    last_pad = [s for s in a1.samples if s.role == "silence_pad"][-1]
    assert last_pad.pcm_samples_contribution == 128  # 128 ticks = 128 pcm
