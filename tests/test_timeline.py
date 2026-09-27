"""时间线内核测试:三个维度分别维护。"""

from pathlib import Path

import numpy as np

from hlsdiff.parser import parse_playlist
from hlsdiff.timeline import build_timeline

FIXTURES = Path(__file__).parent / "fixtures"


def test_timeline_starts_are_cumsum_of_durations():
    pl = parse_playlist((FIXTURES / "window_v1.m3u8").read_text())
    tl = build_timeline(pl)
    np.testing.assert_allclose(tl.starts, [0.0, 6.0, 12.0, 18.0, 24.0])
    np.testing.assert_array_equal(tl.sequences, [0, 1, 2, 3, 4])
    assert tl.total_duration == 30.0


def test_timeline_discontinuity_boundaries():
    pl = parse_playlist((FIXTURES / "discontinuity_v1.m3u8").read_text())
    tl = build_timeline(pl)
    # 媒体序号 0..3,discontinuity 序号 5,5,6,6,时间线起点 0,4,8,12 — 三个维度独立
    np.testing.assert_array_equal(tl.sequences, [0, 1, 2, 3])
    np.testing.assert_array_equal(tl.discontinuity_sequences, [5, 5, 6, 6])
    np.testing.assert_allclose(tl.starts, [0.0, 4.0, 8.0, 12.0])
    np.testing.assert_array_equal(tl.boundary_indices(), [2])


def test_timeline_index_of():
    pl = parse_playlist((FIXTURES / "byterange_v1.m3u8").read_text())
    tl = build_timeline(pl)
    assert tl.index_of(11) == 1
    assert tl.index_of(99) is None


def test_empty_playlist_timeline():
    pl = parse_playlist("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-ENDLIST\n")
    tl = build_timeline(pl)
    assert tl.total_duration == 0.0
    assert tl.boundary_indices().size == 0
