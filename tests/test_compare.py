"""版本对比测试:窗口前移 / 缺段撤回 / 冲突 / 结束后追加。"""

from pathlib import Path

from hlsdiff.compare import compare_versions
from hlsdiff.parser import parse_playlist

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return parse_playlist((FIXTURES / name).read_text())


def test_window_advance_is_not_retraction():
    """v1 序号 0-4,v2 序号 2-6:丢弃 [0,1] 属窗口前移,追加 [5,6],判定 accept。"""
    report = compare_versions(load("window_v1.m3u8"), load("window_v2.m3u8"))
    assert report.decision == "accept"
    assert report.window_advanced == [0, 1]
    assert report.retracted == []
    assert report.appended == [5, 6]
    assert report.conflicts == []


def test_missing_segment_in_window_is_retraction():
    """v2 窗口从 2 起但只有 2,3:序号 4 在窗口内被撤回 → reject。"""
    report = compare_versions(load("window_v1.m3u8"), load("missing_segment_v2.m3u8"))
    assert report.decision == "reject"
    assert report.window_advanced == [0, 1]
    assert report.retracted == [4]
    assert any("content-retracted" in r for r in report.reasons)


def test_seen_sequence_with_different_uri_is_conflict():
    """序号 3 的 URI 由 seg3.ts 变为 seg3-replaced.ts → 冲突单列,判定 conflict。"""
    report = compare_versions(load("window_v1.m3u8"), load("conflict_v2.m3u8"))
    assert report.decision == "conflict"
    assert report.retracted == []
    assert len(report.conflicts) == 1
    c = report.conflicts[0]
    assert c.sequence == 3 and c.field == "uri"
    assert c.old_value == "seg3.ts" and c.new_value == "seg3-replaced.ts"


def test_duration_change_is_conflict():
    v1 = load("window_v1.m3u8")
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:6.0,\nseg0.ts\n#EXTINF:6.0,\nseg1.ts\n"
        "#EXTINF:5.5,\nseg2.ts\n#EXTINF:6.0,\nseg3.ts\n#EXTINF:6.0,\nseg4.ts\n"
    )
    from hlsdiff.parser import parse_playlist

    report = compare_versions(v1, parse_playlist(text))
    assert report.decision == "conflict"
    assert [(c.sequence, c.field) for c in report.conflicts] == [(2, "duration")]


def test_append_after_endlist_rejected():
    """旧版本已 ENDLIST,新版本仍追加序号 1 → reject。"""
    report = compare_versions(load("ended_v1.m3u8"), load("ended_v2_appended.m3u8"))
    assert report.decision == "reject"
    assert report.appended == [1]
    assert any("append-after-endlist" in r for r in report.reasons)


def test_discontinuity_shift_reported_not_rejected():
    v1 = load("discontinuity_v1.m3u8")
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXT-X-DISCONTINUITY-SEQUENCE:7\n"
        "#EXTINF:4.0,\na0.ts\n#EXTINF:4.0,\na1.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXTINF:4.0,\nb0.ts\n#EXTINF:4.0,\nb1.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    from hlsdiff.parser import parse_playlist

    report = compare_versions(v1, parse_playlist(text))
    assert report.decision == "accept"
    assert report.discontinuity_shift == (5, 7)


def test_uri_query_string_redacted_in_conflict():
    """冲突诊断中的 URI 查询串必须脱敏。"""
    from hlsdiff.parser import parse_playlist

    v1 = parse_playlist(
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        "#EXTINF:6.0,\nseg0.ts?token=secret1\n"
    )
    v2 = parse_playlist(
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        "#EXTINF:6.0,\nseg0.ts?token=secret2\n"
    )
    report = compare_versions(v1, v2)
    assert report.decision == "conflict"
    assert report.conflicts[0].old_value == "seg0.ts?<redacted>"
    assert "secret" not in report.conflicts[0].new_value
