"""播放计划测试:可下载计划与连续播放边界。"""

from pathlib import Path

from hlsdiff.parser import parse_playlist
from hlsdiff.plan import build_plan

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return parse_playlist((FIXTURES / name).read_text())


def test_plan_entries_carry_resolved_byte_ranges():
    plan = build_plan(load("byterange_v1.m3u8"))
    assert [ (e.byte_range.offset, e.byte_range.length) for e in plan.entries ] == [
        (0, 100),
        (100, 150),
        (400, 120),
    ]
    assert [e.timeline_start for e in plan.entries] == [0.0, 8.0, 16.0]
    assert [e.timeline_end for e in plan.entries] == [8.0, 16.0, 24.0]
    assert plan.total_duration == 24.0


def test_plan_continuity_boundaries():
    plan = build_plan(load("discontinuity_v1.m3u8"))
    assert len(plan.boundaries) == 1
    b = plan.boundaries[0]
    assert b.index == 2
    assert b.sequence == 2
    assert b.timeline_offset == 8.0
    assert (b.from_discontinuity_sequence, b.to_discontinuity_sequence) == (5, 6)


def test_plan_without_discontinuity_has_no_boundaries():
    plan = build_plan(load("window_v1.m3u8"))
    assert plan.boundaries == []
    assert len(plan.entries) == 5


def test_plan_diagnostics_for_open_and_ended_lists():
    open_plan = build_plan(load("window_v1.m3u8"))
    assert any("未结束" in d for d in open_plan.diagnostics)
    ended_plan = build_plan(load("ended_v1.m3u8"))
    assert any("ENDLIST" in d for d in ended_plan.diagnostics)


def test_empty_playlist_plan_is_undecidable():
    pl = parse_playlist("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-ENDLIST\n")
    plan = build_plan(pl)
    assert plan.entries == []
    assert any("undecidable" in d for d in plan.diagnostics)
