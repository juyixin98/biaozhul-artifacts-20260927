"""播放计划测试：可下载计划与连续播放边界。"""
from hlsplan.diagnostics import DiagnosticLog
from hlsplan.models import FailureCategory
from hlsplan.parser import parse_playlist
from hlsplan.planner import build_plan


def test_plan_resolves_byte_ranges(fixture_text):
    snap = parse_playlist(fixture_text("byterange.m3u8"))
    plan = build_plan(snap)
    assert [e.byte_range for e in plan.entries] == [
        {"offset": 0, "length": 1000},
        {"offset": 1000, "length": 1200},   # 隐式继承
        {"offset": 5000, "length": 800},
    ]
    assert plan.entries[0].map_uri == "init.mp4"
    assert plan.entries[0].map_byte_range == {"offset": 0, "length": 720}
    assert plan.missing_sequences == []


def test_plan_continuous_boundaries_with_discontinuity(fixture_text):
    snap = parse_playlist(fixture_text("discontinuity_v1.m3u8"))
    plan = build_plan(snap)
    # 手工答案：两个连续区间，总时长 8 + 12 = 20
    assert plan.runs == [
        {"start_sequence": 0, "end_sequence": 1, "discontinuity_sequence": 3,
         "start_time": 0.0, "end_time": 8.0, "segment_count": 2},
        {"start_sequence": 2, "end_sequence": 3, "discontinuity_sequence": 4,
         "start_time": 0.0, "end_time": 12.0, "segment_count": 2},
    ]
    assert plan.total_duration == 20.0


def test_plan_reports_missing_and_splits_runs(fixture_text):
    snap = parse_playlist(fixture_text("missing_v2.m3u8"))
    log = DiagnosticLog()
    plan = build_plan(snap, log=log)
    assert plan.missing_sequences == [103]
    assert [(r["start_sequence"], r["end_sequence"]) for r in plan.runs] == [
        (102, 102), (104, 106)]
    codes = {r.code for r in log.records}
    assert FailureCategory.MISSING_SEGMENT.value in codes


def test_plan_incremental_since_sequence(fixture_text):
    snap = parse_playlist(fixture_text("window_v2.m3u8"))
    plan = build_plan(snap, since_sequence=104)
    assert [e.media_sequence for e in plan.entries] == [105, 106]
    # 边界仍基于完整快照
    assert plan.runs[0]["start_sequence"] == 102
    assert plan.runs[0]["end_sequence"] == 106
