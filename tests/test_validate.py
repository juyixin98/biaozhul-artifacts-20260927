"""Independent validator: it must reject tampered plans by exact category.

Plans are built once then deliberately corrupted to prove each container
rule is actually checked, rather than trusting planner-side status.
"""
import copy

from app.core.planner import build_concat_plan
from app.core.validate import validate_plan
from app.errors import FailureCategory
from app.models import ConcatPlan, SegmentRequest


def _plan_ok(load):
    a, b = load("seg_ok_a.json"), load("seg_ok_b.json")
    plan = build_concat_plan(
        [a, b], [SegmentRequest(path=a.name), SegmentRequest(path=b.name)],
        "mp4-constrained")
    assert validate_plan(plan, [a, b]) == []
    return plan, [a, b]


def test_valid_plan_passes(load):
    plan, segs = _plan_ok(load)
    assert validate_plan(plan, segs) == []


def _categories(violations):
    return {v.category for v in violations}


def test_negative_dts_is_detected(load):
    plan, segs = _plan_ok(load)
    d = plan.to_dict()
    d["tracks"][0]["samples"][3]["out_dts"] = -1
    violations = validate_plan(ConcatPlan.from_dict(d), segs)
    assert FailureCategory.NEGATIVE_DTS in _categories(violations)


def test_non_monotonic_dts_is_detected(load):
    plan, segs = _plan_ok(load)
    d = plan.to_dict()
    d["tracks"][0]["samples"][4]["out_dts"] = d["tracks"][0]["samples"][3]["out_dts"]
    violations = validate_plan(ConcatPlan.from_dict(d), segs)
    assert FailureCategory.DTS_NOT_MONOTONIC in _categories(violations)


def test_pts_before_dts_is_detected(load):
    plan, segs = _plan_ok(load)
    d = plan.to_dict()
    s = d["tracks"][0]["samples"][0]
    s["out_pts"] = s["out_dts"] - 1
    violations = validate_plan(ConcatPlan.from_dict(d), segs)
    assert FailureCategory.PTS_BEFORE_DTS in _categories(violations)


def test_dropped_reference_is_detected(load):
    # use the mid-GOP trim plan, then drop the pre-roll P3 (src_index 1)
    from fractions import Fraction
    m = load("seg_midgop.json")
    plan = build_concat_plan(
        [m], [SegmentRequest(path=m.name, trim_in=Fraction(2, 15),
                             trim_out=Fraction(4, 15))],
        "mp4-constrained")
    d = plan.to_dict()
    video = d["tracks"][0]["samples"]
    video[:] = [s for s in video if not (s["segment"] == m.name
                                         and s["src_index"] == 1)]
    violations = validate_plan(ConcatPlan.from_dict(d), [m])
    cats = _categories(violations)
    assert FailureCategory.MISSING_REFERENCE in cats
    missing = next(v for v in violations
                   if v.category is FailureCategory.MISSING_REFERENCE)
    assert missing.context["missing"] == 1


def test_coverage_gap_is_detected(load):
    plan, segs = _plan_ok(load)
    d = plan.to_dict()
    # remove the first presented video frame (I0, src_index 0) from seg A
    video = d["tracks"][0]["samples"]
    video[:] = [s for s in video if not (s["segment"] == "seg_ok_a"
                                         and s["src_index"] == 0)]
    violations = validate_plan(ConcatPlan.from_dict(d), segs)
    # removing I0 also breaks its references, but coverage must fail too
    assert FailureCategory.COVERAGE_GAP in _categories(violations)


def test_padding_not_at_tail_is_detected(load):
    plan, segs = _plan_ok(load)
    d = plan.to_dict()
    audio = d["tracks"][1]["samples"]
    # mark an interior present frame as padding
    audio[5]["role"] = "padding"
    violations = validate_plan(ConcatPlan.from_dict(d), segs)
    assert FailureCategory.PADDING_MISUSE in _categories(violations)


def test_audio_priming_below_edit_list_floor_is_detected(load):
    plan, segs = _plan_ok(load)
    d = plan.to_dict()
    audio = d["tracks"][1]
    audio["samples"][0]["out_pts"] = -audio["edit_list_media_time"] - 1
    violations = validate_plan(ConcatPlan.from_dict(d), segs)
    assert FailureCategory.AUDIO_PRIMING_RANGE in _categories(violations)


def test_segment_keyframe_boundary_is_checked(load):
    from fractions import Fraction
    m = load("seg_midgop.json")
    plan = build_concat_plan(
        [m], [SegmentRequest(path=m.name, trim_in=Fraction(2, 15),
                             trim_out=Fraction(4, 15))],
        "mp4-constrained")
    d = plan.to_dict()
    first = next(s for s in d["tracks"][0]["samples"]
                 if s["segment"] == m.name)
    first["keyframe"] = False
    violations = validate_plan(ConcatPlan.from_dict(d), [m])
    assert FailureCategory.KEYFRAME_BOUNDARY in _categories(violations)


def test_validation_without_segments_skips_reference_check(load):
    plan, _ = _plan_ok(load)
    assert validate_plan(plan, None) == []
