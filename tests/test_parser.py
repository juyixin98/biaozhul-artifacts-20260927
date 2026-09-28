"""解析器测试：结构校验、字节范围继承、失败类别。"""
import pytest

from hlsplan.diagnostics import DiagnosticLog
from hlsplan.models import FailureCategory
from hlsplan.parser import ParseFailure, parse_playlist


def _error_codes(exc: ParseFailure):
    return {r.code for r in exc.log.records if r.severity == "ERROR"}


def test_parse_window_v1(fixture_text):
    snap = parse_playlist(fixture_text("window_v1.m3u8"), name="win")
    assert snap.media_sequence == 100
    assert snap.discontinuity_sequence == 0
    assert snap.target_duration == 4.0
    assert snap.endlist is False
    assert [s.media_sequence for s in snap.segments] == [100, 101, 102, 103, 104]
    assert [s.uri for s in snap.segments] == [
        "seg100.ts", "seg101.ts", "seg102.ts", "seg103.ts", "seg104.ts"]
    assert all(s.duration == 4.0 for s in snap.segments)


def test_discontinuity_sequence_tracked_separately(fixture_text):
    snap = parse_playlist(fixture_text("discontinuity_v1.m3u8"))
    # discontinuity 序号从 3 开始，与媒体序号（从 0 开始）互不影响
    assert [s.media_sequence for s in snap.segments] == [0, 1, 2, 3]
    assert [s.discontinuity_sequence for s in snap.segments] == [3, 3, 4, 4]
    assert [s.discontinuity_before for s in snap.segments] == [False, False, True, False]


def test_byterange_implicit_offset_inherits(fixture_text):
    snap = parse_playlist(fixture_text("byterange.m3u8"))
    segs = snap.segments
    # 显式偏移
    assert (segs[0].byte_range.offset, segs[0].byte_range.length) == (0, 1000)
    # 隐式偏移：继承同一资源上一子范围的 end = 0 + 1000
    assert (segs[1].byte_range.offset, segs[1].byte_range.length) == (1000, 1200)
    # 换了资源，必须显式偏移
    assert (segs[2].byte_range.offset, segs[2].byte_range.length) == (5000, 800)
    # MAP 的 BYTERANGE 缺省偏移按 0
    assert (segs[0].map_byte_range.offset, segs[0].map_byte_range.length) == (0, 720)
    assert segs[0].map_uri == "init.mp4"


def test_byterange_unresolvable_when_resource_changes(fixture_text):
    with pytest.raises(ParseFailure) as ei:
        parse_playlist(fixture_text("byterange_bad.m3u8"))
    assert FailureCategory.BYTERANGE_UNRESOLVABLE.value in _error_codes(ei.value)


def test_duplicate_singleton_tag_rejected(fixture_text):
    with pytest.raises(ParseFailure) as ei:
        parse_playlist(fixture_text("duplicate_tag.m3u8"))
    assert FailureCategory.DUPLICATE_TAG.value in _error_codes(ei.value)


def test_encrypted_playlist_out_of_scope(fixture_text):
    with pytest.raises(ParseFailure) as ei:
        parse_playlist(fixture_text("encrypted.m3u8"))
    assert FailureCategory.ENCRYPTION_UNSUPPORTED.value in _error_codes(ei.value)
    # 诊断中的密钥 URI 必须脱敏，不得出现 session 参数
    for rec in ei.value.log.records:
        assert "SECRETKEY" not in str(rec.detail)


def test_missing_header_rejected():
    with pytest.raises(ParseFailure) as ei:
        parse_playlist("#EXT-X-TARGETDURATION:4\n")
    assert FailureCategory.PARSE_ERROR.value in _error_codes(ei.value)


def test_missing_targetduration_rejected():
    text = "#EXTM3U\n#EXTINF:4.0,\ns0.ts\n"
    with pytest.raises(ParseFailure) as ei:
        parse_playlist(text)
    assert FailureCategory.PARSE_ERROR.value in _error_codes(ei.value)


def test_uri_without_extinf_rejected():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:4\ns0.ts\n"
    with pytest.raises(ParseFailure) as ei:
        parse_playlist(text)
    assert FailureCategory.PARSE_ERROR.value in _error_codes(ei.value)


def test_gap_tag_marks_segment_unavailable(fixture_text):
    snap = parse_playlist(fixture_text("missing_v2.m3u8"))
    # EXT-X-GAP 占位：序号仍然连续分配，103 被标记为不可用
    assert [s.media_sequence for s in snap.segments] == [102, 103, 104, 105, 106]
    assert [s.gap for s in snap.segments] == [False, True, False, False, False]


def test_endlist_parsed(fixture_text):
    snap = parse_playlist(fixture_text("endlist_v1.m3u8"))
    assert snap.endlist is True
    assert snap.playlist_type == "VOD"
    assert snap.last_sequence == 2


def test_parse_success_diagnostic_has_state(fixture_text):
    log = DiagnosticLog()
    parse_playlist(fixture_text("window_v1.m3u8"), log=log)
    parsed = [r for r in log.records if r.code == "PARSED"]
    assert parsed and parsed[0].detail["media_sequence"] == 100
