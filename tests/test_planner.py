"""End-to-end plans: exact, hand-computed per-sample expectations."""
from fractions import Fraction

import pytest

from app.core.planner import build_concat_plan
from app.core.validate import validate_plan
from app.errors import FailureCategory, PlannerError
from app.models import (
    DECISION_DIRECT,
    DECISION_TRANSCODE,
    SegmentRequest,
)


def _req(name, trim_in=None, trim_out=None):
    return SegmentRequest(
        path=name,
        trim_in=trim_in or Fraction(0),
        trim_out=trim_out,
    )


# ---------------------------------------------------------------------------
# direct concat of two compatible segments
# ---------------------------------------------------------------------------

def test_direct_concat_decision_and_tracks(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan([a, b], [_req(a.name), _req(b.name)],
                             "mp4-constrained")
    assert plan.decision is DECISION_DIRECT
    assert [t.stream_type for t in plan.tracks] == ["video", "audio"]
    assert validate_plan(plan, [a, b]) == []


def test_video_dts_relocated_to_zero_and_continuous(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan([a, b], [_req(a.name), _req(b.name)],
                             "mp4-constrained")
    dts = [s.out_dts for s in plan.tracks[0].samples]
    # 20 frames, one continuous DTS series: source DTS (-6000..21000) is
    # shifted so the first output DTS is exactly 0
    assert dts == list(range(0, 60000, 3000))
    assert all(d >= 0 for d in dts)


def test_video_pts_preserves_reorder_offset_after_relocation(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan([a, b], [_req(a.name), _req(b.name)],
                             "mp4-constrained")
    pts = [s.out_pts for s in plan.tracks[0].samples]
    assert pts == [
        6000, 15000, 9000, 12000, 24000, 18000, 21000, 33000, 27000, 30000,
        36000, 45000, 39000, 42000, 54000, 48000, 51000, 63000, 57000, 60000,
    ]
    # decode/presentation offset is preserved per sample
    for s in plan.tracks[0].samples:
        assert s.out_pts - s.out_dts == s.src_pts - s.src_dts


def test_segment_b_starts_at_cursor(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan([a, b], [_req(a.name), _req(b.name)],
                             "mp4-constrained")
    video_b = [s for s in plan.tracks[0].samples if s.segment == b.name]
    assert video_b[0].out_dts == 30000   # cursor after A = 30000 ticks
    audio_b = [s for s in plan.tracks[1].samples if s.segment == b.name]
    assert audio_b[0].out_dts == 17408   # cursor after A = 17*1024


def test_audio_priming_edit_list_and_tail_padding(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan([a, b], [_req(a.name), _req(b.name)],
                             "mp4-constrained")
    track = plan.tracks[1]
    assert track.edit_list_media_time == 1024
    assert track.samples[0].role == "priming"
    assert track.samples[0].out_pts == -1024 and track.samples[0].out_dts == 0
    # second segment carries its own priming frame
    b_priming = [s for s in track.samples
                 if s.segment == b.name and s.role == "priming"]
    assert len(b_priming) == 1
    assert b_priming[0].out_pts == 16384 and b_priming[0].out_dts == 17408
    padding = [s for s in track.samples if s.role == "padding"]
    assert len(padding) == 1
    assert padding[0].out_dts == 33792 and padding[0].out_pts == 32768
    assert padding[0].src_index is None and padding[0].segment == b.name


def test_no_negative_or_duplicate_dts(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan([a, b], [_req(a.name), _req(b.name)],
                             "mp4-constrained")
    for track in plan.tracks:
        dts = [s.out_dts for s in track.samples]
        assert all(d >= 0 for d in dts)
        assert len(set(dts)) == len(dts)


# ---------------------------------------------------------------------------
# non-keyframe trim: pre-roll survives, presentation window is exact
# ---------------------------------------------------------------------------

def test_non_keyframe_trim_keeps_preroll(load):
    m = load("seg_midgop.json")
    req = _req(m.name, trim_in=Fraction(2, 15), trim_out=Fraction(4, 15))
    plan = build_concat_plan([m], [req], "mp4-constrained")
    assert validate_plan(plan, [m]) == []
    video = plan.tracks[0].samples
    assert [(s.src_index, s.role) for s in video] == [
        (0, "preroll"), (1, "preroll"), (4, "present"), (5, "present"),
        (6, "present"), (7, "preroll"), (8, "present"),
    ]
    assert [s.out_dts for s in video] == [0, 3000, 12000, 15000,
                                          18000, 21000, 24000]
    # the B-frame at 21000 (P9, presented after the window) is retained
    # purely as a reference
    p9 = next(s for s in video if s.src_index == 7)
    assert p9.role == "preroll" and p9.out_pts == 33000
    audio = plan.tracks[1].samples
    assert [s.src_index for s in audio] == [7, 8, 9, 10, 11, 12, 13]
    assert audio[0].role == "priming"
    assert [s.out_dts for s in audio] == list(range(0, 7168, 1024))


# ---------------------------------------------------------------------------
# open GOP cut at the recovery point
# ---------------------------------------------------------------------------

def test_open_gop_plan_does_not_drop_required_reference(load):
    o = load("seg_open_gop.json")
    req = _req(o.name, trim_in=Fraction(1, 5))   # pts 18000 = K6
    plan = build_concat_plan([o], [req], "mp4-constrained")
    assert validate_plan(plan, [o]) == []
    video = plan.tracks[0].samples
    assert [s.src_index for s in video] == [0, 1, 4, 7, 8, 9]
    assert {s.src_index for s in video if s.role == "preroll"} == {0, 1}
    # B7/B8's backward reference P3 (index 1) is present in the output
    assert [s.out_dts for s in video] == [0, 3000, 12000,
                                          21000, 24000, 27000]


# ---------------------------------------------------------------------------
# 2048-sample audio encoder delay
# ---------------------------------------------------------------------------

def test_two_frame_encoder_delay_plan(load):
    d = load("seg_audio_delay.json")
    plan = build_concat_plan([d], [_req(d.name)], "mp4-constrained")
    assert validate_plan(plan, [d]) == []
    audio = plan.tracks[1]
    assert audio.edit_list_media_time == 2048
    priming = [s for s in audio.samples if s.role == "priming"]
    assert [(s.src_index, s.out_dts, s.out_pts) for s in priming] == [
        (0, 0, -2048), (1, 1024, -1024)]
    assert all(s.out_dts >= 0 for s in audio.samples)
    assert [s.role for s in audio.samples].count("padding") == 0


# ---------------------------------------------------------------------------
# incompatible inputs: explicit transcode decision, never a faked plan
# ---------------------------------------------------------------------------

def test_timebase_mismatch_forces_transcode(load):
    a = load("seg_ok_a.json")
    tb = load("seg_tb_mismatch.json")
    plan = build_concat_plan([a, tb], [_req(a.name), _req(tb.name)],
                             "mp4-constrained")
    assert plan.decision is DECISION_TRANSCODE
    assert plan.tracks == []
    assert any(r["category"] == "TIMEBASE_MISMATCH"
               and r["field"] == "time_base" for r in plan.reasons)
    assert all(r["requirement"] == "transcode" for r in plan.reasons)


def test_codec_mismatch_forces_transcode(load):
    a = load("seg_ok_a.json")
    hc = load("seg_codec_mismatch.json")
    plan = build_concat_plan([a, hc], [_req(a.name), _req(hc.name)],
                             "mp4-constrained")
    assert plan.decision is DECISION_TRANSCODE
    assert any(r["category"] == "CODEC_MISMATCH" and r["field"] == "codec"
               for r in plan.reasons)


def test_dangling_reference_fails_categorically(load):
    d = load("seg_dangling_ref.json")
    with pytest.raises(PlannerError) as exc:
        build_concat_plan([d], [_req(d.name)], "mp4-constrained")
    assert exc.value.category is FailureCategory.MISSING_REFERENCE
    assert exc.value.context["missing_index"] == 99


def test_segment_windows_recorded_per_stream(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan([a, b], [_req(a.name), _req(b.name)],
                             "mp4-constrained")
    windows = {w.segment: w for w in plan.segment_windows}
    assert windows[a.name].windows["video"] == {"in_tick": 0, "out_tick": 30000}
    assert windows[b.name].windows["audio"] == {"in_tick": 0, "out_tick": 16000}
    assert windows[a.name].sha256 == a.sha256
