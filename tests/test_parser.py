"""解析器测试:具体结果与失败类别断言。期望值均为手工计算的参考答案。"""

from pathlib import Path

import pytest

from hlsdiff.errors import FailureCategory, PlaylistParseError
from hlsdiff.parser import parse_playlist

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_basic_sequences_and_durations():
    pl = parse_playlist(load("window_v1.m3u8"))
    assert pl.media_sequence == 0
    assert pl.target_duration == 6.0
    assert not pl.endlist
    assert [s.sequence for s in pl.segments] == [0, 1, 2, 3, 4]
    assert [s.uri for s in pl.segments] == [f"seg{i}.ts" for i in range(5)]
    assert all(s.duration == 6.0 for s in pl.segments)


def test_byte_range_implicit_offset_inheritance():
    """byterange_v1: 100@0, 150(隐式), 120@400 → 绝对偏移 0 / 100 / 400。"""
    pl = parse_playlist(load("byterange_v1.m3u8"))
    ranges = [(s.byte_range.offset, s.byte_range.length) for s in pl.segments]
    assert ranges == [(0, 100), (100, 150), (400, 120)]
    assert [s.sequence for s in pl.segments] == [10, 11, 12]


def test_byte_range_implicit_offset_requires_same_uri():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:8\n"
        "#EXTINF:8.0,\n#EXT-X-BYTERANGE:100@0\na.ts\n"
        "#EXTINF:8.0,\n#EXT-X-BYTERANGE:50\nb.ts\n"  # 换了 URI 且无显式偏移
    )
    with pytest.raises(PlaylistParseError) as ei:
        parse_playlist(text)
    assert ei.value.category is FailureCategory.BYTE_RANGE_OFFSET_UNRESOLVABLE


def test_duplicate_singleton_tag_rejected():
    with pytest.raises(PlaylistParseError) as ei:
        parse_playlist(load("duplicate_tag.m3u8"))
    assert ei.value.category is FailureCategory.DUPLICATE_TAG


def test_content_after_endlist_rejected():
    with pytest.raises(PlaylistParseError) as ei:
        parse_playlist(load("append_after_endlist.m3u8"))
    assert ei.value.category is FailureCategory.CONTENT_AFTER_ENDLIST


def test_missing_extm3u_rejected():
    with pytest.raises(PlaylistParseError) as ei:
        parse_playlist("#EXT-X-TARGETDURATION:6\n")
    assert ei.value.category is FailureCategory.MISSING_EXTM3U


def test_uri_without_extinf_rejected():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\nseg0.ts\n"
    with pytest.raises(PlaylistParseError) as ei:
        parse_playlist(text)
    assert ei.value.category is FailureCategory.MISSING_EXTINF


def test_dangling_extinf_rejected():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\n"
    with pytest.raises(PlaylistParseError) as ei:
        parse_playlist(text)
    assert ei.value.category is FailureCategory.DANGLING_EXTINF


def test_encrypted_playlist_out_of_scope():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        "#EXTINF:6.0,\nseg0.ts\n"
    )
    with pytest.raises(PlaylistParseError) as ei:
        parse_playlist(text)
    assert ei.value.category is FailureCategory.ENCRYPTION_UNSUPPORTED


def test_key_method_none_accepted():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        '#EXT-X-KEY:METHOD=NONE\n'
        "#EXTINF:6.0,\nseg0.ts\n"
    )
    pl = parse_playlist(text)
    assert len(pl.segments) == 1


def test_discontinuity_sequence_tracked_separately():
    """discontinuity_v1: 起始 dseq=5,第 3 段前有 DISCONTINUITY → dseq 序列 5,5,6,6。"""
    pl = parse_playlist(load("discontinuity_v1.m3u8"))
    assert pl.discontinuity_sequence == 5
    assert [s.discontinuity_sequence for s in pl.segments] == [5, 5, 6, 6]
    assert [s.discontinuity for s in pl.segments] == [False, False, True, False]
    assert [s.sequence for s in pl.segments] == [0, 1, 2, 3]  # 媒体序号不受断点影响
