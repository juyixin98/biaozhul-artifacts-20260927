"""规划器行为测试：逐样本断言 + 具体失败类别断言。

期望常量均来自夹具的手工设计（见 scripts/build_fixtures.py 注释），
不是由被测规划器生成。
"""
from __future__ import annotations

import pytest

from mediaconcat.media import parse_sidecar
from mediaconcat.models import FailureCode, Severity
from mediaconcat.planner import plan_concat


def codes(plan, severity=Severity.ERROR):
    return [f.code for f in plan.findings if f.severity == severity]


def seg(plan, clip_index, media):
    return next(s for s in plan.segments
                if s.clip_index == clip_index and s.stream == media)


# ------------------------------------------------------- 案例 1：混合时基

def test_mixed_timebases_convert_losslessly_and_join():
    a = parse_sidecar("fixtures/tb_mixed_a.media.json")
    b = parse_sidecar("fixtures/tb_mixed_b.media.json")
    plan = plan_concat([a, b], "mp4", job_id="t1")

    assert plan.feasible is True
    assert plan.mode == "concat_copy"
    assert plan.output_time_base == (1, 150)  # lcm(25,30)
    assert codes(plan) == []

    s0, s1 = seg(plan, 0, "video"), seg(plan, 1, "video")
    # clip0：25fps → ×6，7 帧占 42 ticks
    assert [s.out_dts for s in s0.samples] == [6 * i for i in range(7)]
    assert s0.samples[0].duration == 6
    # clip1：30fps → ×5，无缝拼接，起点恰为 42，不重叠不留缝
    assert s1.samples[0].out_dts == 42
    assert [s.out_dts for s in s1.samples] == [42 + 5 * i for i in range(7)]
    assert s1.samples[-1].out_dts + s1.samples[-1].duration == 77
    # 输出总时长 = 7/25 + 7/30 = 0.513333...
    assert plan.output_duration_sec == pytest.approx(7 / 25 + 7 / 30, abs=1e-9)


def test_bframe_reorder_offsets_survive_relocation():
    a = parse_sidecar("fixtures/tb_mixed_a.media.json")
    b = parse_sidecar("fixtures/tb_mixed_b.media.json")
    plan = plan_concat([a, b], "mp4", job_id="t1b")
    s0 = seg(plan, 0, "video")
    # 解码序 I0 P3 B1 B2 P6 B4 B5；逐帧 (PTS-DTS) 偏移：
    # 帧1=P3 偏移2，帧2=B1 偏移-1，帧3=B2 偏移-1（1/25 网格下相邻 B 帧偏移相同）
    src_offsets = [0, 2, -1, -1, 2, -1, -1]
    got = [s.out_pts - s.out_dts for s in s0.samples]
    assert got == [6 * x for x in src_offsets]
    # 呈现序严格递增
    pts = sorted(s.out_pts for s in s0.samples)
    assert pts == [6 * i for i in range(7)]


# ------------------------------------------------- 案例 2：开放 GOP + 预滚

def test_open_gop_keeps_required_reference_without_negative_dts():
    clip = parse_sidecar("fixtures/open_gop.media.json")
    plan = plan_concat([clip], "mp4", job_id="t2")

    assert plan.feasible is True
    assert codes(plan) == []
    s0 = seg(plan, 0, "video")
    # 从非关键帧6起切。帧6 refs=[5,4]（开放GOP跨GOP引用）；帧4 又依赖 3→2→1→0，
    # 完整参考闭包必须回溯到上一个真正的重启点帧0，因此保留全部 0..9，
    # 其中 0..5 为预滚（丢掉它们中任何一帧，帧4/帧6 都无法解码）。
    assert s0.retained_src_indices == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    roles = {s.src_index: s.role for s in s0.samples}
    assert all(roles[i] == "preroll_reference" for i in (0, 1, 2, 3, 4, 5))
    assert all(roles[i] == "content" for i in (6, 7, 8, 9))
    assert s0.preroll_count == 6
    # 关键不变量：无负 DTS，解码序严格递增
    dts = [s.out_dts for s in s0.samples]
    assert dts[0] == 0
    assert all(x > 0 for x in dts[1:])
    assert dts == sorted(dts)
    # 内容呈现从帧6（0.24s）开始；内容为帧6..9，结束帧10，时长 4 帧 = 0.16s
    assert plan.output_duration_sec == pytest.approx(0.16, abs=1e-9)
    # 必需参考帧没有丢：内容帧6的 references=[5,4] 均保留
    assert clip.video.frames[6].references == [5, 4]
    assert next(s for s in s0.samples if s.src_index == 6).role == "content"


def test_open_gop_reference_lost_at_concat_boundary_is_error():
    a = parse_sidecar("fixtures/open_gop_lost_a.media.json")
    b = parse_sidecar("fixtures/open_gop_lost_b.media.json")
    plan = plan_concat([a, b], "mp4", job_id="t2b")

    assert plan.feasible is False
    assert plan.mode == "transcode_required"
    assert plan.segments == []
    errs = plan.findings
    assert codes(plan) == [FailureCode.OPEN_GOP_REFERENCE_LOST]
    f = errs[0]
    assert f.clip_index == 1 and f.stream == "video"
    # 证据必须可复核：外部引用 -1（上一片段最后一帧）来自片段b的内容帧1
    assert f.evidence["external_references"] == [-1]


# ---------------------------------------------- 案例 3：AAC 编码器延迟

def test_aac_encoder_delay_marked_per_packet():
    clip = parse_sidecar("fixtures/aac_delay.media.json")
    plan = plan_concat([clip], "mp4", job_id="t3")

    assert plan.feasible is True
    s0 = seg(plan, 0, "audio")
    by_role = {
        role: [s for s in s0.samples if s.role == role]
        for role in ("drop_encoder_delay", "content")
    }
    # 2112 = 2*1024+64：包0/1 全弃，包2 留 960，逐样本贡献
    contribs = [s.pcm_samples_contribution for s in s0.samples]
    assert contribs == [0, 0, 960, 1024, 1024, 1024, 1024, 1024]
    assert len(by_role["drop_encoder_delay"]) == 3
    assert s0.samples[2].note
    # 可听时长 = (8192-2112)/48000 = 0.126666...
    assert plan.output_duration_sec == pytest.approx((8192 - 2112) / 48000, abs=1e-9)
    assert all(s.out_dts >= 0 for s in s0.samples)


def test_aac_delay_requires_transcode_in_mpegts():
    # 时基 1/90000 本身满足 TS 时钟；拒绝原因只能是 priming 无法在 TS 中表达
    clip = parse_sidecar("fixtures/aac_delay_ts.media.json")
    plan = plan_concat([clip], "mpegts", job_id="t3b")
    assert plan.feasible is False
    assert codes(plan) == [FailureCode.CONTAINER_CONSTRAINT]
    f = plan.findings[0]
    assert f.stream == "audio"
    assert f.evidence["encoder_delay"] == 2112
    assert f.evidence["container"] == "mpegts"


# ---------------------------------------------- 案例 4：非关键帧裁剪

def test_non_keyframe_cut_at_boundary_is_error_not_fake_copy():
    a = parse_sidecar("fixtures/nonkey_a.media.json")
    b = parse_sidecar("fixtures/nonkey_b.media.json")
    plan = plan_concat([a, b], "mp4", job_id="t4")
    assert plan.feasible is False
    assert plan.mode == "transcode_required"
    assert codes(plan) == [FailureCode.NON_KEYFRAME_CUT]
    f = plan.findings[0]
    assert f.clip_index == 1
    # 0.08s @25fps => 目标帧2，最近关键帧是0
    assert f.evidence["first_content_frame"] == 2
    assert f.evidence["nearest_keyframe"] == 0


def test_first_clip_non_keyframe_cut_allows_preroll():
    # open_gop 夹具同样以 0.2s（帧5）起切；首片段允许预滚而非报错
    clip = parse_sidecar("fixtures/open_gop.media.json")
    plan = plan_concat([clip], "mp4", job_id="t4b")
    warns = [f for f in plan.findings if f.severity == Severity.WARNING]
    assert any(f.code == FailureCode.NON_KEYFRAME_CUT for f in warns)
    assert plan.feasible is True


# ---------------------------------------------- 案例 5：音频尾部填充

def test_audio_tail_padding_to_video_duration():
    clip = parse_sidecar("fixtures/av_tailpad.media.json")
    plan = plan_concat([clip], "mp4", job_id="t5")
    assert plan.feasible is True

    v = seg(plan, 0, "video")
    a = seg(plan, 0, "audio")
    # 统一时基 1/48000：视频 8 帧 × 25fps → 每帧 1920 ticks，结束 15360
    assert v.samples[-1].out_dts + v.samples[-1].duration == 15360
    pads = [s for s in a.samples if s.role == "silence_pad"]
    assert a.pad_count == 3 and len(pads) == 3
    # 12 个源包到 12288；3 个填充包 (12288 起 1024/包) 覆盖到 15360
    assert [p.out_dts for p in pads] == [12288, 13312, 14336]
    assert all(p.pcm_samples_contribution == 1024 for p in pads)
    # 输出时长以视频为准 0.32s
    assert plan.output_duration_sec == pytest.approx(0.32, abs=1e-9)
    # 填充后段内 DTS 仍严格递增
    dts = [s.out_dts for s in a.samples]
    assert dts == sorted(set(dts))
    assert codes(plan) == []
    assert FailureCode.AV_DURATION_MISMATCH in codes(plan, Severity.WARNING)


# ---------------------------------------------- 案例 6：容器时钟约束

def test_mpegts_rejects_non_divisible_timebase():
    clip = parse_sidecar("fixtures/tb12800.media.json")
    plan = plan_concat([clip], "mpegts", job_id="t6")
    assert plan.feasible is False
    assert codes(plan) == [FailureCode.CONTAINER_CONSTRAINT]
    assert plan.findings[0].evidence == {
        "clock": 90000, "bad_source_denominators": [12800]
    }


def test_mp4_accepts_12800_timebase():
    clip = parse_sidecar("fixtures/tb12800.media.json")
    plan = plan_concat([clip], "mp4", job_id="t6b")
    assert plan.feasible is True
    assert plan.output_time_base == (1, 12800)
    s0 = seg(plan, 0, "video")
    # 每帧 duration 512 ticks
    assert [s.duration for s in s0.samples] == [512, 512, 512]
    assert [s.out_dts for s in s0.samples] == [0, 512, 1024]


# ---------------------------------------------- 参数不兼容：必须转码

def test_codec_mismatch_is_transcode(tmp_path):
    a = parse_sidecar("fixtures/tb_mixed_a.media.json")
    b = parse_sidecar("fixtures/tb12800.media.json")  # 同为 h264 但不同时基
    # 构造编码不一致：手工改 codec
    b.video.codec = "hevc"
    plan = plan_concat([a, b], "mp4", job_id="t7")
    assert plan.feasible is False
    assert FailureCode.CODEC_MISMATCH in codes(plan)


def test_empty_input_is_explicit_failure():
    plan = plan_concat([], "mp4", job_id="t8")
    assert plan.feasible is False
    assert codes(plan) == [FailureCode.EMPTY_INPUT]


def test_multi_clip_av_padding_does_not_overlap_boundary():
    # 回归：第二段音频填充必须在计算下一段游标前加入，避免段间 DTS 重叠
    clips = [parse_sidecar("fixtures/av_tailpad.media.json") for _ in range(2)]
    plan = plan_concat(clips, "mp4", job_id="t9")
    assert plan.feasible is True
    a0, a1 = seg(plan, 0, "audio"), seg(plan, 1, "audio")
    assert a0.pad_count == a1.pad_count == 3
    # 第一段填充结束 15360 == 第二段起点 15360（无缝、不重叠）
    assert a0.samples[-1].out_dts == 14336
    assert a1.samples[0].out_dts == 15360
    assert plan.output_duration_sec == pytest.approx(0.64, abs=1e-9)
    dts = [s.out_dts for s in plan.segments if s.stream == "audio" for s in [s] for s in s.samples]
    assert dts == sorted(dts) and len(dts) == len(set(dts))


def test_non_frame_boundary_video_cut_requires_transcode():
    clip = parse_sidecar("fixtures/nonkey_a.media.json")
    clip.cut_in_sec = 0.071  # @25fps → 1.775 ticks，不落在任何帧边界
    plan = plan_concat([clip], "mp4", job_id="t10")
    assert plan.feasible is False
    assert FailureCode.INVALID_RANGE in codes(plan)
