"""时间与信号内核测试：期望值为手工计算的参考答案。"""
import numpy as np

from hlsplan.parser import parse_playlist
from hlsplan.timeline import continuous_runs, find_missing_sequences, start_times


def test_start_times_prefix_sum():
    # 手工答案：[4,4,6] -> [0,4,8]
    np.testing.assert_array_equal(
        start_times(np.array([4.0, 4.0, 6.0])), np.array([0.0, 4.0, 8.0]))
    assert start_times(np.array([])).size == 0
    assert start_times(np.array([2.5]))[0] == 0.0


def test_continuous_runs_split_on_discontinuity(fixture_text):
    snap = parse_playlist(fixture_text("discontinuity_v1.m3u8"))
    runs = continuous_runs(snap.segments)
    assert len(runs) == 2
    # 手工答案：run0 = 序号0-1，disc序号3，时间 0..8；run1 = 序号2-3，disc序号4，时间 0..12
    r0, r1 = runs
    assert (r0.start_sequence, r0.end_sequence) == (0, 1)
    assert r0.discontinuity_sequence == 3
    assert (r0.start_time, r0.end_time) == (0.0, 8.0)
    assert (r1.start_sequence, r1.end_sequence) == (2, 3)
    assert r1.discontinuity_sequence == 4
    assert (r1.start_time, r1.end_time) == (0.0, 12.0)


def test_continuous_runs_split_on_gap(fixture_text):
    snap = parse_playlist(fixture_text("missing_v2.m3u8"))
    runs = continuous_runs(snap.segments)
    # 缺 103：run0 = 102，run1 = 104-106
    assert [(r.start_sequence, r.end_sequence) for r in runs] == [(102, 102), (104, 106)]


def test_find_missing_sequences(fixture_text):
    snap = parse_playlist(fixture_text("missing_v2.m3u8"))
    assert find_missing_sequences(snap.segments) == [103]
    ok = parse_playlist(fixture_text("window_v2.m3u8"))
    assert find_missing_sequences(ok.segments) == []
