"""拼接规划器：在不重编码（stream-copy）前提下逐样本决定可行性与时间重定位。

判定分两级：
- error：直拼不可行，计划 ``mode=transcode_required``，并给出具体类别；
- warning：计划仍可直拼，但附带明确条件（如音频尾部填充、A/V 时长差）。

规划的每个输出样本都带有源 index、源/输出 DTS、PTS 与角色，
因此“仅生成计划”也可逐样本检验。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

import numpy as np

from . import kernel
from .config import Settings, settings as default_settings
from .logging_setup import LogBinding, bind
from .models import (
    ClipProbe,
    ConcatPlan,
    FailureCode as FC,
    Finding,
    PlannedSample,
    SegmentPlan,
    Severity as Sev,
    StreamProbe,
)

MP4_LIKE = {"mp4", "fmp4", "mov", "mkv"}
TS_LIKE = {"mpegts", "ts", "m2ts"}
# 仅 MP4 家族能通过 edit-list（elst）在本规划器的模型内表达 AAC priming。
# MKV 另有 CodecDelay/DiscardPadding 机制，但当前模型未承载，保守按不支持处理；
# MPEG-TS 无任何 priming 表达。
PRIMING_CONTAINERS = {"mp4", "fmp4", "mov"}


# ---------------------------------------------------------------- 数据辅助


@dataclass
class _Selection:
    media: str
    streams: list[StreamProbe]          # 每个片段一条（缺失为 None 时不会进入这里）
    time_base: tuple[int, int]
    out_tb: tuple[int, int]
    segments: list[SegmentPlan]
    local: list[dict]                   # 与 segments 对齐的中间量


def _finding(severity: Sev, code: FC, message: str, ev: dict) -> Finding:
    ev = dict(ev)
    return Finding(
        code=code, severity=severity, message=message,
        stream=ev.pop("stream", None), clip_index=ev.pop("clip_index", None),
        sample_index=ev.pop("sample_index", None), evidence=ev,
    )


def _err(code: FC, message: str, **ev) -> Finding:
    return _finding(Sev.ERROR, code, message, ev)


def _warn(code: FC, message: str, **ev) -> Finding:
    return _finding(Sev.WARNING, code, message, ev)


def _clip_media_streams(clips: list[ClipProbe], media: str) -> list[Optional[StreamProbe]]:
    return [clip.video if media == "video" else clip.audio for clip in clips]


# ---------------------------------------------------------- 1. 存在性与参数


def check_presence(clips: list[ClipProbe]) -> list[Finding]:
    findings: list[Finding] = []
    for media in ("video", "audio"):
        present = [(i, s) for i, s in enumerate(_clip_media_streams(clips, media)) if s]
        if not present:
            continue
        if len(present) != len(clips):
            missing = [i for i in range(len(clips)) if i not in {i for i, _ in present}]
            findings.append(
                _err(
                    FC.CODEC_MISMATCH,
                    f"{media} 流在部分片段缺失：片段 {missing} 无 {media} 流",
                    media=media,
                    missing_clips=missing,
                )
            )
    return findings


def _conservative_field_check(streams, field: str, code: FC, label: str,
                              media: str) -> Optional[Finding]:
    """参数一致性：只要有片段提供了该字段且取值不统一就拒绝直拼。

    全部缺失（无法确认）不在此拦截；关键 SPS/PPS 等 extradata 一致性由
    extradata_id 承担（合成夹具显式提供，ffprobe 路径缺失时保守拒绝）。
    """
    values = {getattr(s, field) for s in streams}
    known = {v for v in values if v is not None}
    if len(known) > 1:
        return _err(code, f"{media} {label}不一致：{sorted(map(str, known))}，必须转码统一",
                    media=media, field=field, values=sorted(map(str, known)))
    return None


def check_params(clips: list[ClipProbe]) -> list[Finding]:
    findings: list[Finding] = []
    for media in ("video", "audio"):
        streams = _clip_media_streams(clips, media)
        if not all(streams):
            continue
        present: list[StreamProbe] = [s for s in streams if s]  # type: ignore[list-item]

        codecs = {s.codec for s in present}
        if len(codecs) > 1:
            findings.append(_err(
                FC.CODEC_MISMATCH,
                f"{media} 编码不兼容：{sorted(codecs)}，直拼要求编码一致",
                media=media, codecs=sorted(codecs),
            ))

        if media == "video":
            dims = {(s.width, s.height) for s in present}
            if len(dims) > 1:
                findings.append(_err(
                    FC.VIDEO_PARAM_MISMATCH,
                    f"视频分辨率不一致：{sorted(map(str, dims))}，必须转码统一",
                    media=media, dimensions=[list(d) for d in dims],
                ))
            for field, code, label in (
                ("pixel_format", FC.VIDEO_PARAM_MISMATCH, "像素格式"),
                ("profile", FC.VIDEO_PARAM_MISMATCH, "profile"),
                ("level", FC.VIDEO_PARAM_MISMATCH, "level"),
                ("sample_aspect_ratio", FC.VIDEO_PARAM_MISMATCH, "采样宽高比"),
            ):
                f = _conservative_field_check(present, field, code, label, "video")
                if f:
                    findings.append(f)
            # extradata（SPS/PPS）：至少一个片段提供了标识就必须全部一致
            ids = {s.extradata_id for s in present if s.extradata_id is not None}
            if len(ids) > 1:
                findings.append(_err(
                    FC.VIDEO_PARAM_MISMATCH,
                    f"视频 SPS/PPS(extradata) 不一致：{sorted(ids)}，直拼会引用错误参数集，必须转码",
                    media=media, extradata_ids=sorted(ids),
                ))
        else:
            rates = {s.sample_rate for s in present}
            chans = {s.channels for s in present}
            if len(rates) > 1 or len(chans) > 1:
                findings.append(_err(
                    FC.AUDIO_PARAM_MISMATCH,
                    f"音频采样率/声道不一致：rates={sorted(rates)} channels={sorted(chans)}",
                    media=media,
                    sample_rates=sorted(r for r in rates if r),
                    channels=sorted(c for c in chans if c),
                ))
            for field, label in (
                ("audio_object_type", "AAC object type"),
                ("profile", "音频 profile"),
            ):
                f = _conservative_field_check(present, field,
                                              FC.AUDIO_PARAM_MISMATCH, label, "audio")
                if f:
                    findings.append(f)
            ids = {s.extradata_id for s in present if s.extradata_id is not None}
            if len(ids) > 1:
                findings.append(_err(
                    FC.AUDIO_PARAM_MISMATCH,
                    f"音频 AudioSpecificConfig(extradata) 不一致：{sorted(ids)}，必须转码",
                    media=media, extradata_ids=sorted(ids),
                ))
    return findings


# ------------------------------------------------------------- 2. 裁剪解析


def _video_content_set(stream: StreamProbe, clip: ClipProbe, clip_index: int,
                       findings: list[Finding]) -> Optional[set[int]]:
    """在**呈现序（PTS）**上确定内容帧集合（解码序 index 集合）。

    B 帧重排使得解码序与呈现序不同，因此裁剪定界必须按 PTS 排序后进行，
    再映射回解码序索引；否则 cut_in/cut_out 会错误地丢掉/纳入 B、P 帧。
    非帧边界（不落在 tick 网格或没有对应 PTS）记 INVALID_RANGE。
    """
    frames = stream.frames
    n = len(frames)
    if n == 0:
        findings.append(_err(FC.EMPTY_INPUT, "视频流没有任何帧",
                             clip_index=clip_index, stream="video"))
        return None
    n_t, d_t = stream.time_base
    pts0 = min(f.pts for f in frames)
    # 呈现序：按 PTS 排序的解码序索引
    present_order = sorted(range(n), key=lambda i: frames[i].pts)

    def target_pts(seconds: float, kind: str) -> Optional[int]:
        ticks, on_grid = kernel.seconds_to_ticks_exact(seconds, stream.time_base)
        target = pts0 + ticks
        has = any(frames[i].pts == target for i in present_order)
        if not on_grid or not has:
            nearest_i = min(present_order, key=lambda i: abs(frames[i].pts - target))
            findings.append(_err(
                FC.INVALID_RANGE,
                f"视频{kind}点 {seconds:.6f}s 不落在任何帧边界"
                f"（最近帧 PTS={(frames[nearest_i].pts - pts0) * n_t / d_t:.6f}s），"
                "直拼无法精确裁剪，必须转码",
                clip_index=clip_index, stream="video",
                requested_seconds=seconds,
                nearest_frame_seconds=(frames[nearest_i].pts - pts0) * n_t / d_t,
            ))
            return None
        return target

    lo_present, hi_present = 0, n  # 呈现序 [lo, hi)
    if clip.cut_in_sec is not None:
        t = target_pts(clip.cut_in_sec, "入")
        if t is None:
            return None
        lo_present = next(k for k in range(n) if frames[present_order[k]].pts >= t)
    if clip.cut_out_sec is not None:
        t = target_pts(clip.cut_out_sec, "出")
        if t is None:
            return None
        hi_present = next(k for k in range(n) if frames[present_order[k]].pts >= t)
    if lo_present >= hi_present:
        findings.append(_err(FC.INVALID_RANGE, "视频裁剪区间为空",
                             clip_index=clip_index, stream="video"))
        return None
    return {present_order[k] for k in range(lo_present, hi_present)}


def _plan_video_segment(
    clip: ClipProbe, stream: StreamProbe, clip_index: int,
    findings: list[Finding], output_container: str = "mp4",
) -> tuple[Optional[SegmentPlan], dict]:
    """单片段视频：呈现序定界 → 关键帧边界判定 → 完整参考闭包。"""
    if stream is None or not stream.frames:
        findings.append(_err(FC.EMPTY_INPUT, "视频流缺失或为空",
                             clip_index=clip_index, stream="video"))
        return None, {}
    frames = stream.frames
    n = len(frames)
    content = _video_content_set(stream, clip, clip_index, findings)
    if content is None:
        return None, {}

    present_order = sorted(range(n), key=lambda i: frames[i].pts)
    first_present = present_order[0]
    # 内容在呈现序中的首/尾帧（入点帧、出点前最后一帧的解码序）
    k_first = next(k for k in range(n) if present_order[k] in content)
    k_last = max(k for k in range(n) if present_order[k] in content)
    head_content = present_order[k_first]

    refs = {i: list(frames[i].references) for i in range(n)}
    key_set = {i for i in range(n) if frames[i].keyframe}
    # 真正的解码器重启点：关键帧且**不引用任何其它帧**（干净 IDR）。
    # 只要引用了帧——更早帧（开放GOP/CRA）、自身（畸形自引用）、更晚帧（乱序）、
    # 或源外帧——都不能保证从该帧独立起解码，因而不构成回溯停止点/边界起点。
    restart_points = {i for i in key_set if not refs.get(i)}

    # 完整参考闭包（在真正的重启点终止）。先算出“源内”闭包与越界引用。
    closure_internal = kernel.reference_closure(
        content, refs, stop_at=restart_points
    )
    out_of_range = sorted(r for r in closure_internal if r < 0 or r >= n)

    # 拼接入点判定（S1）：无论是否声明 cut_in，clip_index>0 的首呈现内容帧
    # 都必须能从片段边界起独立解码。
    is_boundary = clip_index > 0
    needs_earlier = any(r < head_content for r in closure_internal if 0 <= r < n)
    starts_with_restart = head_content in restart_points

    if is_boundary:
        if out_of_range:
            findings.append(_err(
                FC.OPEN_GOP_REFERENCE_LOST,
                f"片段 {clip_index} 引用了上一片段/源外样本 {out_of_range}，拼接会丢失必需参考",
                clip_index=clip_index, stream="video",
                external_references=out_of_range,
            ))
            return None, {}
        # ffprobe 等路径无法确认逐帧引用闭合：即使首帧带关键帧标志，也无法
        # 排除开放 GOP/CRA，拼接边界不能放行 → 保守要求转码。
        if not getattr(stream, "reference_info_complete", True):
            findings.append(_err(
                FC.GOP_STRUCTURE_UNKNOWN,
                f"片段 {clip_index} 的逐帧 GOP/参考结构不可用（如 ffprobe 不暴露"
                " 开放 GOP 引用），无法确认边界关键帧为可独立解码的闭合 GOP，"
                "拼接必须转码；若确为闭合 IDR 边界请提供显式逐帧元数据",
                clip_index=clip_index, stream="video",
                first_content_frame=head_content,
            ))
            return None, {}
        if needs_earlier or not starts_with_restart:
            anchor = max((r for r in restart_points if r < head_content), default=None)
            findings.append(_err(
                FC.NON_KEYFRAME_CUT,
                f"片段 {clip_index} 的拼接入点帧 #{head_content} 不是可独立起解码的"
                f"关键帧（最近重启点 #{anchor}），拼接边界禁止非关键帧/开放GOP直拼，必须转码",
                clip_index=clip_index, stream="video",
                first_content_frame=head_content, nearest_keyframe=anchor,
            ))
            return None, {}

    # 首片段：允许保留预滚；越界引用仍是错误
    if out_of_range:
        findings.append(_err(
            FC.OPEN_GOP_REFERENCE_LOST,
            f"片段 {clip_index} 必需参考样本 {out_of_range} 不在源中",
            clip_index=clip_index, stream="video", lost_references=out_of_range,
        ))
        return None, {}

    if needs_earlier:
        findings.append(_warn(
            FC.NON_KEYFRAME_CUT,
            f"首片段从帧 #{head_content} 起切，保留其完整参考链作为预滚（含开放GOP跨GOP帧），"
            "时间轴已重定位消除负 DTS",
            clip_index=clip_index, stream="video",
            first_content_frame=head_content,
        ))

    retained = closure_internal
    # L7：MPEG-TS 不支持负 PTS。首片段预滚帧中若有呈现早于内容起点的帧，
    # 重定位后该预滚帧 PTS 为负 → TS 无法承载，必须转码。
    if output_container.lower() in TS_LIKE and needs_earlier:
        min_retained_pts = min(frames[i].pts for i in retained)
        content_start_pts = frames[head_content].pts
        if min_retained_pts < content_start_pts:
            findings.append(_err(
                FC.CONTAINER_CONSTRAINT,
                f"片段 {clip_index} 预滚样本产生负 PTS（B帧呈现早于入点 "
                f"{content_start_pts - min_retained_pts} ticks），MPEG-TS 不支持负时间戳，必须转码",
                clip_index=clip_index, stream="video",
                negative_pts_ticks=content_start_pts - min_retained_pts,
                container=output_container,
            ))
            return None, {}

    first_retained = min(retained)
    last_retained = max(retained) + 1

    samples: list[PlannedSample] = []
    for i in range(first_retained, last_retained):
        f = frames[i]
        role = "content" if i in content else "preroll_reference"
        samples.append(PlannedSample(
            clip_index=clip_index, stream="video", src_index=i,
            src_dts=f.dts, src_pts=f.pts, duration=f.duration,
            out_dts=0, out_pts=0, keyframe=f.keyframe, role=role,
        ))

    preroll_count = sum(1 for s in samples if s.role == "preroll_reference")
    segment = SegmentPlan(
        clip_index=clip_index, source=clip.source, stream="video",
        output_start_dts=0, output_start_pts=0,
        preroll_count=preroll_count,
        content_count=len(content),
        first_content_src_index=min(content),
        retained_src_indices=sorted(retained),
        samples=samples,
    )
    local = {
        "first_retained": first_retained,
        "last_retained_excl": last_retained,
        "first_content": min(content),
        "last_content": max(content) + 1,
        "content_set": content,
        "present_last": present_order[k_last],
    }
    return segment, local


# ------------------------------------------------------------- 3. 音频段


def _audio_pcm_cut(stream: StreamProbe, clip: ClipProbe, clip_index: int,
                   findings: list[Finding]) -> Optional[tuple[int, int]]:
    """以 PCM 样本域精确裁剪；入/出点必须落在包解码输出边界。"""
    frames = stream.frames
    if not frames:
        findings.append(_err(FC.EMPTY_INPUT, "音频流没有任何包",
                             clip_index=clip_index, stream="audio"))
        return None
    if not stream.sample_rate:
        findings.append(_err(FC.AUDIO_PARAM_MISMATCH,
                             "音频流缺少 sample_rate，无法在 PCM 样本域定界",
                             clip_index=clip_index, stream="audio"))
        return None
    rate = stream.sample_rate
    starts: list[int] = []
    acc = 0
    for f in frames:
        starts.append(acc)
        acc += f.pcm_samples or 0
    # 可听样本域起点 = encoder delay 之后
    audible0 = stream.encoder_delay_samples

    first, last = 0, len(frames)
    # cut_in_sec==0.0 与 None 等价（从起点开始），不应触发 priming 清零误判
    cut_in = None if clip.cut_in_sec in (None, 0.0) else clip.cut_in_sec
    cut_out = None if clip.cut_out_sec in (None, 0.0) else clip.cut_out_sec

    def target_pcm(seconds: float) -> tuple[int, bool]:
        frac = Fraction(str(seconds)) * rate
        return int(frac), frac.denominator == 1

    if cut_in is not None:
        t, integral = target_pcm(cut_in)
        target = audible0 + t
        if not integral or target not in starts:
            findings.append(_err(
                FC.INVALID_RANGE,
                f"音频入点 {cut_in:.6f}s 不落在 AAC 包 PCM 边界 "
                f"(target={target} samples, rate={rate})，直拼无法样本级精确裁剪",
                clip_index=clip_index, stream="audio",
                requested_seconds=cut_in, target_pcm=target,
                packet_starts=starts[:8],
            ))
            return None
        first = next(i for i, s in enumerate(starts) if s >= target)
    if cut_out is not None:
        t, integral = target_pcm(cut_out)
        target = audible0 + t
        if not integral or target not in starts:
            findings.append(_err(
                FC.INVALID_RANGE,
                f"音频出点 {cut_out:.6f}s 不落在 AAC 包 PCM 边界 "
                f"(target={target} samples, rate={rate})",
                clip_index=clip_index, stream="audio",
                requested_seconds=cut_out, target_pcm=target,
            ))
            return None
        last = next(i for i, s in enumerate(starts) if s >= target)
    if first >= last:
        findings.append(_err(FC.INVALID_RANGE, "音频裁剪区间为空",
                             clip_index=clip_index, stream="audio"))
        return None
    return first, last


def _plan_audio_segment(
    clip: ClipProbe, stream: StreamProbe, clip_index: int,
    container: str, findings: list[Finding],
) -> tuple[Optional[SegmentPlan], dict]:
    bounds = _audio_pcm_cut(stream, clip, clip_index, findings)
    if bounds is None:
        return None, {}
    first_pkt, last_pkt = bounds

    frames = stream.frames[first_pkt:last_pkt]
    pcm = np.array([f.pcm_samples or 0 for f in frames], dtype=np.int64)

    # priming 仅在未从头裁掉时存在；MP4 edit-list 可表达首段 priming。
    # 多片段拼接时后续段无法用单一 edit-list 逐段平移（M4）→ 预检阶段判转码。
    cut_from_start = clip.cut_in_sec not in (None, 0.0)
    delay = stream.encoder_delay_samples if not cut_from_start else 0
    contributions, roles = kernel.priming_drop(delay, pcm)

    samples: list[PlannedSample] = []
    for off, f in enumerate(frames):
        samples.append(PlannedSample(
            clip_index=clip_index, stream="audio",
            src_index=first_pkt + off,
            src_dts=f.dts, src_pts=f.pts, duration=f.duration,
            out_dts=0, out_pts=0, keyframe=True,
            role="drop_encoder_delay" if roles[off] else "content",
            pcm_samples_contribution=int(contributions[off]),
            note=f"priming {delay}" if roles[off] else "",
        ))

    segment = SegmentPlan(
        clip_index=clip_index, source=clip.source, stream="audio",
        output_start_dts=0, output_start_pts=0, preroll_count=0,
        content_count=len(frames),
        first_content_src_index=first_pkt,
        retained_src_indices=list(range(first_pkt, last_pkt)),
        samples=samples,
    )
    local = {
        "first_pkt": first_pkt,
        "last_pkt": last_pkt,
        "audible_pcm": int(contributions.sum()),
        "encoder_delay": delay,
    }
    return segment, local


# ------------------------------------------------------------- 4. 装配时间轴


def _attach_output_times(
    segment: SegmentPlan, local: dict, stream: StreamProbe,
    out_tb: tuple[int, int], shift_out: int,
) -> None:
    """把段内样本映射到输出时间轴；shift_out 为段首保留样本 DTS 对应的输出 tick。"""
    frames = stream.frames
    first_retained = local["first_retained"] if segment.stream == "video" else local["first_pkt"]
    base_dts = frames[first_retained].dts
    src_tb = stream.time_base
    factor = kernel.reduce_factor(kernel.scale_factor(src_tb, out_tb))

    for s in segment.samples:
        f = frames[s.src_index]
        rel_dts = f.dts - base_dts
        rel_pts = f.pts - base_dts
        s.out_dts = shift_out + rel_dts * factor[0] // factor[1]
        s.out_pts = shift_out + rel_pts * factor[0] // factor[1]
        s.duration = f.duration * factor[0] // factor[1]

    segment.output_start_dts = segment.samples[0].out_dts
    segment.output_start_pts = segment.samples[0].out_pts


def _align_audio_to_video_boundary(
    segment: SegmentPlan, stream: StreamProbe, out_tb: tuple[int, int],
    boundary_start_out: int, video_boundary_end_out: int,
    findings: list[Finding], clip_index: int,
) -> None:
    """把该段音频可听内容对齐到视频拼接边界 [boundary_start, video_boundary_end]。

    可听内容（priming 扣减后）从该段拼接边界开始；其**段长**与视频内容段长比较：
    - 更长 → 直拼无法删音频包对齐 → error（转码）；
    - 更短 → 追加静音帧桥接到视频边界；最后一个可为部分贡献帧精确收边。
    填充包放在真实包之后，DTS 严格递增、段间不重叠。
    """
    if not stream.frames or not stream.sample_rate:
        return
    rate = stream.sample_rate
    frame_pcm = stream.frames[0].pcm_samples or 1024
    frame_ticks_out, frame_rem = kernel.audio_frame_is_integral(frame_pcm, rate, out_tb)
    if frame_rem:
        findings.append(_err(
            FC.TIMEBASE_INCOMPATIBLE,
            f"片段 {clip_index} 音频帧 {frame_pcm} PCM 在输出时基 "
            f"{kernel.fraction_str(out_tb)} 下非整数 ticks（余数 {frame_rem}），"
            "直拼会产生时间戳漂移，必须转码/重采样",
            clip_index=clip_index, stream="audio", frame_pcm=frame_pcm,
            output_time_base=list(out_tb),
        ))
        return

    audible_out = 0
    for s in segment.samples:
        if s.role in ("content", "drop_encoder_delay"):
            c = s.pcm_samples_contribution if s.pcm_samples_contribution is not None else frame_pcm
            audible_out += frame_ticks_out * c // frame_pcm
    video_segment_len = video_boundary_end_out - boundary_start_out
    gap = video_segment_len - audible_out
    if gap < 0:
        findings.append(_err(
            FC.AV_DURATION_MISMATCH,
            f"片段 {clip_index} 可听音频内容比视频长 {-gap} 输出 ticks，"
            "直拼无法在拼接边界对齐两轨（需删除音频包→重编码），必须转码",
            clip_index=clip_index, stream="audio",
            audio_overhang_ticks=-gap,
        ))
        return
    if gap == 0:
        return

    last_real_end = max(
        s.out_dts + s.duration for s in segment.samples if s.src_index >= 0
    )
    # 静音帧放在真实包末尾到视频边界之间（绝不越过边界，避免与下一片段重叠）。
    # 按 frame 长切分，最后一段可为不足整帧的部分贡献帧。
    end = video_boundary_end_out
    placements: list[tuple[int, int, int]] = []  # (start, pcm_contribution, piece_ticks)
    cursor = last_real_end
    while end - cursor > 0:
        piece = min(end - cursor, frame_ticks_out)
        pcm_piece = piece * frame_pcm // frame_ticks_out
        placements.append((cursor, pcm_piece, piece))
        cursor += piece
    for start, pcm_piece, piece in placements:
        is_partial = piece < frame_ticks_out
        segment.samples.append(PlannedSample(
            clip_index=clip_index, stream="audio", src_index=-1,
            src_dts=0, src_pts=0, duration=piece,
            out_dts=start, out_pts=start, keyframe=True,
            role="silence_pad", pcm_samples_contribution=pcm_piece,
            note=("synthetic silence to match video duration"
                  if not is_partial
                  else f"partial silence pad: {piece} ticks / {pcm_piece} pcm"),
        ))
    segment.pad_count = len(placements)
    residual = (end - last_real_end) % frame_ticks_out
    findings.append(_warn(
        FC.AV_DURATION_MISMATCH,
        f"片段 {clip_index} 音频短于视频 {gap} 输出 ticks，追加 {segment.pad_count} 个静音帧"
        f"（末帧部分贡献 {residual} ticks）对齐视频边界",
        clip_index=clip_index, gap_ticks=gap,
        silence_packets=segment.pad_count, residual_ticks=residual,
    ))


# ------------------------------------------------------------- 5. 主入口


def plan_concat(
    clips: list[ClipProbe],
    output_container: str = "mp4",
    job_id: str = "job",
    settings: Settings = default_settings,
    log: Optional[LogBinding] = None,
) -> ConcatPlan:
    log = (log or bind()).bind(job_id=job_id)
    findings: list[Finding] = []

    def fail_plan(tb: tuple[int, int] = (1, 1)) -> ConcatPlan:
        plan = ConcatPlan(
            job_id=job_id, feasible=False, mode="transcode_required",
            output_container=output_container, output_time_base=tb,
            clips=[c.source for c in clips], findings=findings,
        )
        log.decision(
            "plan_complete", decision="transcode_required",
            basis={"errors": [f.code.value for f in plan.errors()],
                   "warnings": [f.code.value for f in plan.findings
                                 if f.severity == Sev.WARNING]},
        )
        return plan

    if not clips:
        f = _err(FC.EMPTY_INPUT, "未提供任何输入片段")
        log.decision("plan_complete", decision="transcode_required",
                     basis={"errors": [f.code.value]})
        return ConcatPlan(
            job_id=job_id, feasible=False, mode="transcode_required",
            output_container=output_container, output_time_base=(1, 1),
            clips=[], findings=[f],
        )

    findings += check_presence(clips)
    findings += check_params(clips)

    # 统一输出时基：跨全部流（视频+音频）取 LCM。MP4 各轨可共享同一时标，
    # 统一单位也是跨媒体对齐（尾部填充、输出时长）的前提。
    all_streams = [s for clip in clips for s in (clip.video, clip.audio) if s is not None]
    all_tbs = sorted({s.time_base for s in all_streams})
    common_tb: Optional[tuple[int, int]] = None
    try:
        common_tb = kernel.choose_output_timebase(
            all_tbs, output_container,
            mpegts_clock=settings.mpegts_clock,
            max_timescale=settings.max_timescale,
        )
    except kernel.TimebaseSelectionError as exc:
        code = FC.CONTAINER_CONSTRAINT if "clock" in exc.reason else FC.TIMEBASE_INCOMPATIBLE
        findings.append(_err(code, f"输出时基与容器 {output_container} 不兼容：{exc.reason}",
                             **exc.evidence))

    selections: dict[str, _Selection] = {}
    for media in ("video", "audio"):
        streams = _clip_media_streams(clips, media)
        if not all(streams):
            continue
        present: list[StreamProbe] = [s for s in streams if s]  # type: ignore[list-item]
        tbs = sorted({s.time_base for s in present})
        if common_tb is None:
            # 统一时基已在公共阶段产出错误；这里占位，稍后随错误一并提前返回
            out_tb = (1, 1)
        else:
            out_tb = common_tb
        # 每条源时基必须可无损换算到统一时基（统一时基不可用时公共阶段已报错）
        if common_tb is None:
            selections[media] = _Selection(
                media=media, streams=present, time_base=tbs[0],
                out_tb=(1, 1), segments=[], local=[],
            )
            continue
        out_tb = common_tb
        for s in present:
            if not kernel.conversion_is_lossless(s.time_base, out_tb):
                findings.append(_err(
                    FC.TIMEBASE_INCOMPATIBLE,
                    f"{media} 源时基 {kernel.fraction_str(s.time_base)} 到输出 "
                    f"{kernel.fraction_str(out_tb)} 存在取整余数，必须重采样时间基（转码）",
                    stream=media,  # type: ignore[arg-type]
                    source_time_base=list(s.time_base),
                    output_time_base=list(out_tb),
                ))
            # M5：时间戳换算结果必须能放入 int64（DTS/PTS 字段宽度）
            if s.frames:
                peak = max(abs(f.dts) for f in s.frames)
                if not kernel.scaled_ticks_fit_int64(peak, s.time_base, out_tb):
                    findings.append(_err(
                        FC.TIMEBASE_CONVERSION_OVERFLOW,
                        f"{media} 时间戳换算到 {kernel.fraction_str(out_tb)} 超出 int64，"
                        "无法在容器中表达，必须转码/重定位",
                        stream=media,  # type: ignore[arg-type]
                        peak_src_dts=peak,
                        source_time_base=list(s.time_base),
                        output_time_base=list(out_tb),
                    ))
            # L6：音频 PCM 帧长在输出时基下必须是整数 ticks（如 AAC@44100→1/90000 不是）
            if media == "audio" and s.frames and s.sample_rate:
                frame_pcm = s.frames[0].pcm_samples or 0
                if frame_pcm:
                    _, rem = kernel.audio_frame_is_integral(frame_pcm, s.sample_rate, out_tb)
                    if rem:
                        findings.append(_err(
                            FC.TIMEBASE_INCOMPATIBLE,
                            f"音频帧 {frame_pcm} PCM 在输出时基 {kernel.fraction_str(out_tb)} "
                            f"（rate={s.sample_rate}）下非整数 ticks，直拼会累积漂移，必须转码",
                            stream="audio",
                            frame_pcm=frame_pcm, sample_rate=s.sample_rate,
                            output_time_base=list(out_tb),
                        ))
        selections[media] = _Selection(
            media=media, streams=present, time_base=tbs[0],
            out_tb=out_tb, segments=[], local=[],
        )

    # 错误已达可行性门槛：先不装配段，但仍返回全部判定
    if [f for f in findings if f.severity == Sev.ERROR]:
        tb = selections.get("video", selections.get("audio")).out_tb if selections else (1, 1)
        return fail_plan(tb)

    # 逐片段、逐媒体规划
    for ci, clip in enumerate(clips):
        log.progress("plan_clip", (ci + 1) / len(clips), clip=clip.source, clip_index=ci)
        # AAC priming：容器表达能力（M3）+ 多片段逐段平移不可行（M4）
        if clip.audio is not None and clip.cut_in_sec in (None, 0.0) \
                and clip.audio.encoder_delay_samples > 0:
            if output_container.lower() not in PRIMING_CONTAINERS:
                findings.append(_err(
                    FC.CONTAINER_CONSTRAINT,
                    f"容器 {output_container} 无法表达 {clip.audio.encoder_delay_samples} "
                    "样本的编码器延迟修剪（无 MP4 edit-list），AAC priming 会在开头产生噪声，必须转码",
                    clip_index=ci, stream="audio",
                    encoder_delay=clip.audio.encoder_delay_samples,
                    container=output_container,
                ))
            elif ci > 0:
                findings.append(_err(
                    FC.PRIMING_TRIM_UNSUPPORTED,
                    f"片段 {ci} 带 {clip.audio.encoder_delay_samples} 样本 AAC priming，"
                    "拼接输出只有单一 edit-list，无法对后续片段逐段平移 priming，"
                    "会累积 A/V 偏移，必须转码",
                    clip_index=ci, stream="audio",
                    encoder_delay=clip.audio.encoder_delay_samples,
                ))
        for media, sel in selections.items():
            stream = clip.video if media == "video" else clip.audio
            if media == "video":
                seg, loc = _plan_video_segment(
                    clip, stream, ci, findings, output_container)  # type: ignore[arg-type]
            else:
                seg, loc = _plan_audio_segment(
                    clip, stream, ci, output_container, findings)  # type: ignore[arg-type]
            if seg is not None:
                sel.segments.append(seg)
                sel.local.append(loc)

    if [f for f in findings if f.severity == Sev.ERROR]:
        tb = next(iter(selections.values())).out_tb if selections else (1, 1)
        return fail_plan(tb)

    # 装配输出时间轴（两遍，保证音频能看到视频边界）：
    #  视频先重定位确定每片段拼接边界，音频再重定位并用静音填充对齐到该边界。
    media_order = [m for m in ("video", "audio") if m in selections]
    cursors = {m: 0 for m in media_order}
    video_boundary: dict[int, int] = {}
    for media in media_order:
        sel = selections[media]
        for seg, loc in zip(sel.segments, sel.local):
            ci = seg.clip_index
            stream = clips[ci].video if media == "video" else clips[ci].audio
            frames = stream.frames  # type: ignore[union-attr]
            first_retained = loc["first_retained"] if media == "video" else loc["first_pkt"]
            first_content = loc["first_content"] if media == "video" else loc["first_pkt"]
            factor = kernel.reduce_factor(
                kernel.scale_factor(stream.time_base, sel.out_tb))  # type: ignore[union-attr]

            base_dts = frames[first_retained].dts
            first_content_dts = frames[first_content].dts
            head_preroll = (first_content_dts - base_dts) * factor[0] // factor[1]
            shift = 0 if ci == 0 else cursors[media] - head_preroll
            _attach_output_times(seg, loc, stream, sel.out_tb, shift)  # type: ignore[arg-type]
            bitstream_start_out = seg.samples[0].out_dts

            if media == "video":
                min_content_pts = min(frames[i].pts for i in loc["content_set"])
                content_start_out = bitstream_start_out + (
                    min_content_pts - base_dts
                ) * factor[0] // factor[1]
                end_pts = max(frames[i].pts + frames[i].duration
                              for i in loc["content_set"])
                span = (end_pts - min_content_pts) * factor[0] // factor[1]
                boundary_end = cursors["video"] + span
                video_boundary[ci] = boundary_end
                cursors["video"] = boundary_end
                loc["_content_start_out"] = content_start_out
                loc["_content_end_out"] = boundary_end
            else:
                first_content_pts = frames[first_content].pts
                content_start_out = bitstream_start_out + (
                    first_content_pts - base_dts
                ) * factor[0] // factor[1]
                if ci in video_boundary:
                    _align_audio_to_video_boundary(
                        seg, stream, sel.out_tb, cursors["audio"],
                        video_boundary[ci], findings, ci,
                    )
                    cursors["audio"] = video_boundary[ci]
                    loc["_content_end_out"] = video_boundary[ci]
                else:
                    cursors["audio"] = seg.samples[-1].out_pts + seg.samples[-1].duration
                    loc["_content_end_out"] = cursors["audio"]
                loc["_content_start_out"] = content_start_out
            loc["_dts_cursor_after"] = cursors[media]
            log.step(
                "segment_timeline", "attach", clip_index=ci, media=media,
                bitstream_start=seg.output_start_dts,
                content_start=content_start_out,
                dts_cursor_after=cursors[media],
            )

    # 装配阶段（填充/多轨对齐）可能新增错误：立即降级，不产出伪装可行的计划
    if [f for f in findings if f.severity == Sev.ERROR]:
        tb = next(iter(selections.values())).out_tb if selections else (1, 1)
        return fail_plan(tb)

    # 输出时长（呈现坐标）：
    # - 含视频时以视频内容呈现结束为准（音频向上取整的填充可能多出不足一个
    #   音频帧的尾部 overhang，由容器在视频结束处截断，不应计入声明时长）；
    # - 纯音频时以“可听内容 + 静音填充”结束为准。
    out_tb = next(iter(selections.values())).out_tb

    def _audio_audible_end(sel) -> int:
        last_seg = sel.segments[-1]
        astream = clips[last_seg.clip_index].audio
        frame_pcm = astream.frames[0].pcm_samples  # type: ignore[union-attr]
        audible = 0
        for s in last_seg.samples:
            if s.role in ("content", "drop_encoder_delay"):
                contribution = s.pcm_samples_contribution if s.pcm_samples_contribution is not None else frame_pcm
                audible += s.duration * contribution // frame_pcm
            elif s.role == "silence_pad":
                audible += s.duration
        start = next(
            loc["_content_start_out"]
            for seg, loc in zip(sel.segments, sel.local) if seg is last_seg
        )
        return start + audible

    if "video" in selections:
        # 视频边界结束已是从输出 0 起算的绝对 ticks（游标从 0 累加内容 span）。
        # 首片段预滚的呈现由 edit-list 隐藏，内容起点即输出 0。
        end_ticks = selections["video"].local[-1]["_content_end_out"]
    else:
        a_origin = 0
        a_starts = [loc["_content_start_out"] for loc in selections["audio"].local]
        if a_starts:
            a_origin = min(a_starts)
        end_ticks = _audio_audible_end(selections["audio"]) - a_origin
    duration_sec = end_ticks * out_tb[0] / out_tb[1]

    all_segments = [s for sel in selections.values() for s in sel.segments]

    invariants = _compute_invariants(all_segments)
    plan = ConcatPlan(
        job_id=job_id, feasible=True, mode="concat_copy",
        output_container=output_container, output_time_base=out_tb,
        clips=[c.source for c in clips], findings=findings,
        segments=all_segments, output_duration_sec=round(duration_sec, 9),
        invariants=invariants,
    )
    verify_plan_invariants(plan, findings)
    verify_with_sources(plan, clips, findings)
    # 校验若引入新错误，则计划降级
    if [f for f in findings if f.severity == Sev.ERROR]:
        plan.feasible = False
        plan.mode = "transcode_required"
        plan.segments = []
        plan.findings = findings
    log.decision(
        "plan_complete",
        decision=plan.mode,
        basis={"errors": len(plan.errors()), "warnings": len(findings) - len(plan.errors())},
        duration_sec=plan.output_duration_sec, samples=sum(len(s.samples) for s in all_segments),
    )
    return plan


# ------------------------------------------------------------- 6. 不变量校验


def _compute_invariants(segments: list[SegmentPlan]) -> dict:
    """逐样本不变量摘要（无错误时供外部快速复核）。"""
    inv: dict = {"total_samples": 0, "per_stream": {}}
    for seg in segments:
        st = inv["per_stream"].setdefault(seg.stream, {
            "samples": 0, "min_dts": None, "max_dts_end": None,
            "nonnegative": True, "strict_increasing": True,
            "preroll_reference": 0, "silence_pad": 0, "drop_encoder_delay": 0,
        })
        dts = [s.out_dts for s in seg.samples]
        st["samples"] += len(dts)
        inv["total_samples"] += len(dts)
        if dts:
            st["min_dts"] = dts[0] if st["min_dts"] is None else min(st["min_dts"], dts[0])
            st["max_dts_end"] = (
                seg.samples[-1].out_dts + seg.samples[-1].duration
                if st["max_dts_end"] is None
                else max(st["max_dts_end"], seg.samples[-1].out_dts + seg.samples[-1].duration)
            )
            st["nonnegative"] &= all(d >= 0 for d in dts)
            st["strict_increasing"] &= all(b > a for a, b in zip(dts, dts[1:]))
        for role in ("preroll_reference", "silence_pad", "drop_encoder_delay"):
            st[role] += sum(1 for s in seg.samples if s.role == role)
    return inv


def verify_plan_invariants(plan: ConcatPlan, findings: list[Finding]) -> None:
    """对装配好的计划做独立校验：负 DTS / 回退 / 重复 / 有损换算。"""
    for media in ("video", "audio"):
        segs = [s for s in plan.segments if s.stream == media]
        if not segs:
            continue
        dts = np.array([s.out_dts for seg in segs for s in seg.samples], dtype=np.int64)
        bad = kernel.assert_monotonic_nonnegative(dts)
        if bad is not None:
            findings.append(_err(
                FC.NEGATIVE_DTS,
                f"{media} 输出时间轴在第 {bad} 个样本出现负/回退 DTS={int(dts[bad])}",
                stream=media,  # type: ignore[arg-type]
                sample_position=bad, dts=int(dts[bad]),
            ))
        # 段边界重叠：下一段首包 DTS 必须不早于上段末包结束（DTS+duration）
        prev_end: Optional[int] = None
        for seg in segs:
            if prev_end is not None and seg.samples and seg.samples[0].out_dts < prev_end:
                findings.append(_err(
                    FC.NEGATIVE_DTS,
                    f"{media} 片段 {seg.clip_index} 与前段在 DTS={prev_end} 处重叠",
                    stream=media,  # type: ignore[arg-type]
                    clip_index=seg.clip_index, boundary_end=prev_end,
                    next_start=seg.samples[0].out_dts,
                ))
                break
            if seg.samples:
                prev_end = seg.samples[-1].out_dts + seg.samples[-1].duration

        # B 帧重排正确性：每段内容样本按呈现序（out_pts）必须严格递增，
        # 且解码序中允许 PTS 回退（回退量即 CTS）。
        for seg in segs:
            present_pts = sorted(
                s.out_pts for s in seg.samples
                if s.src_index >= 0 and s.role != "preroll_reference"
            )
            if any(b <= a for a, b in zip(present_pts, present_pts[1:])):
                findings.append(_err(
                    FC.NEGATIVE_DTS,
                    f"{media} 片段 {seg.clip_index} 呈现时间戳非严格递增",
                    stream=media,  # type: ignore[arg-type]
                    clip_index=seg.clip_index,
                ))


def verify_with_sources(
    plan: ConcatPlan, clips: list[ClipProbe], findings: list[Finding]
) -> None:
    """需要源数据的逐样本校验：重排偏移保持、无损换算、参考闭包完整。"""
    out_tb = plan.output_time_base
    for seg in plan.segments:
        clip = clips[seg.clip_index]
        stream = clip.video if seg.stream == "video" else clip.audio
        if stream is None:
            findings.append(_err(
                FC.INTERNAL_ERROR,
                f"片段 {seg.clip_index} 的 {seg.stream} 段在源中找不到对应流",
                clip_index=seg.clip_index, stream=seg.stream,
            ))
            continue
        factor = kernel.reduce_factor(kernel.scale_factor(stream.time_base, out_tb))
        # 1) DTS/PTS/duration 无损换算（余数必须为 0）
        for s in seg.samples:
            if s.src_index < 0:
                continue
            f = stream.frames[s.src_index]
            expect_dts, rem_d = kernel.convert_ticks(f.dts, stream.time_base, out_tb)
            expect_pts, rem_p = kernel.convert_ticks(f.pts, stream.time_base, out_tb)
            if rem_d or rem_p:
                findings.append(_err(
                    FC.TIMEBASE_INCOMPATIBLE,
                    f"{seg.stream} 样本 clip={seg.clip_index} idx={s.src_index} "
                    "时基换算存在余数，禁止直拼",
                    stream=seg.stream, clip_index=seg.clip_index,
                    sample_index=s.src_index, remainder_dts=rem_d, remainder_pts=rem_p,
                ))
        # 2) 重排偏移保持：out_pts-out_dts == 缩放后的 (src_pts-src_dts)
        base_src_dts = stream.frames[seg.retained_src_indices[0]].dts
        for s in seg.samples:
            if s.src_index < 0:
                continue
            f = stream.frames[s.src_index]
            rel_dts = (f.dts - base_src_dts) * factor[0] // factor[1]
            rel_pts = (f.pts - base_src_dts) * factor[0] // factor[1]
            if (s.out_pts - s.out_dts) != (rel_pts - rel_dts):
                findings.append(_err(
                    FC.NEGATIVE_DTS,
                    f"{seg.stream} 样本 clip={seg.clip_index} idx={s.src_index} "
                    "重排偏移（PTS-DTS）未保持",
                    stream=seg.stream, clip_index=seg.clip_index,
                    sample_index=s.src_index,
                    out_offset=s.out_pts - s.out_dts,
                    expected_offset=rel_pts - rel_dts,
                ))
        # 3) 视频解码参考闭包：对**所有保留样本**（含 preroll_reference）校验，
        #    其引用的源帧必须都在保留集合内。不能跳过预滚帧（否则闭包盲区）。
        if seg.stream == "video":
            retained = set(seg.retained_src_indices)
            n = len(stream.frames)
            # 重启点集合（与规划阶段同一定义），用于解释闭包终止是否合法
            for s in seg.samples:
                if s.src_index < 0:
                    continue
                for ref in stream.frames[s.src_index].references:
                    if ref < 0 or ref >= n:
                        findings.append(_err(
                            FC.OPEN_GOP_REFERENCE_LOST,
                            f"样本 clip={seg.clip_index} idx={s.src_index} 引用越界帧 {ref}",
                            clip_index=seg.clip_index, stream="video",
                            sample_index=s.src_index, missing_reference=ref,
                        ))
                    elif ref not in retained:
                        findings.append(_err(
                            FC.OPEN_GOP_REFERENCE_LOST,
                            f"样本 clip={seg.clip_index} idx={s.src_index} "
                            f"（{s.role}）必需参考帧 {ref} 未保留，参考闭包不完整",
                            clip_index=seg.clip_index, stream="video",
                            sample_index=s.src_index, role=s.role,
                            missing_reference=ref,
                        ))
            # 4) 闭包充分性：保留集合必须等于“内容帧的完整参考闭包”。
            #    这里用**独立的朴素 DFS**（不调用规划阶段所用的 kernel.reference_closure）
            #    交叉重算，避免与被测规划逻辑共用同一实现而双双出错。
            refs_map = {i: list(stream.frames[i].references) for i in range(n)}
            restart = {
                i for i in range(n)
                if stream.frames[i].keyframe and not refs_map.get(i)
            }
            content_idx = {s.src_index for s in seg.samples
                           if s.role == "content" and s.src_index >= 0}
            expected: set[int] = set()
            stack = list(content_idx)
            while stack:
                node = stack.pop()
                if node in expected:
                    continue
                expected.add(node)
                if node in restart:
                    continue
                if 0 <= node < n:
                    for rf in refs_map.get(node, []):
                        if rf not in expected:
                            stack.append(rf)
                # node 越界（<0 / >=n）也保留在 expected 中，下方按缺失暴露
            expected_internal = {r for r in expected if 0 <= r < n}
            missing = expected_internal - retained
            out_of_range_expected = sorted(r for r in expected if r < 0 or r >= n)
            for ref in out_of_range_expected:
                findings.append(_err(
                    FC.OPEN_GOP_REFERENCE_LOST,
                    f"片段 {seg.clip_index} 独立校验发现越界引用 {ref}（独立重算）",
                    clip_index=seg.clip_index, stream="video",
                    missing_reference=ref,
                ))
            for ref in sorted(missing):
                findings.append(_err(
                    FC.OPEN_GOP_REFERENCE_LOST,
                    f"片段 {seg.clip_index} 参考闭包缺少必需帧 {ref}（独立重算）",
                    clip_index=seg.clip_index, stream="video",
                    missing_reference=ref,
                ))
