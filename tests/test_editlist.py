"""edit list 呈现映射测试：空编辑、裁剪边界、多段编辑、未覆盖样本。"""

from fractions import Fraction

import pytest

from mp4timeline.mp4parse.parser import parse_movie
from mp4timeline.timeline.kernel import build_track_timeline


def _timeline(generated_dir, name):
    movie = parse_movie((generated_dir / name).read_bytes(), name)
    return build_track_timeline(movie.tracks[0], movie.movie_timescale)


class TestEmptyEdit:
    def test_leading_empty_edit_shifts_presentation(self, generated_dir, testlog):
        tl = _timeline(generated_dir, "empty_edit.mp4")
        actual = [(p.sample_index, p.movie_time, p.movie_duration) for p in tl.presentations]
        expected = [
            (0, Fraction(500), Fraction(250)),
            (1, Fraction(750), Fraction(250)),
            (2, Fraction(1000), Fraction(250)),
            (3, Fraction(1250), Fraction(250)),
        ]
        testlog.write(
            {
                "event": "assertion",
                "test": "leading_empty_edit",
                "basis": "手写参考 fixtures/references/empty_edit.json："
                "空编辑(500,-1)使电影游标前进500，媒体[0,1000)映射到电影[500,1500)",
                "expected": [[i, str(m), str(d)] for i, m, d in expected],
                "actual": [[i, str(m), str(d)] for i, m, d in actual],
            }
        )
        assert actual == expected
        assert tl.movie_span == (Fraction(500), Fraction(1500))
        assert tl.unpresented == ()


class TestTrimEdit:
    def test_segment_boundaries(self, generated_dir, testlog):
        """段起点闭、段终点开：PTS=250 包含，PTS=750（恰为段终点）排除。"""

        tl = _timeline(generated_dir, "trim_edit.mp4")
        presented = {p.sample_index: p.movie_time for p in tl.presentations}
        unpresented = [u.sample_index for u in tl.unpresented]
        testlog.write(
            {
                "event": "assertion",
                "test": "trim_edit_boundaries",
                "basis": "手写参考 trim_edit.json：段[250,750)起点闭终点开",
                "expected": {"presented": {1: "0", 2: "250"}, "unpresented": [0, 3]},
                "actual": {
                    "presented": {k: str(v) for k, v in presented.items()},
                    "unpresented": unpresented,
                },
            }
        )
        assert presented == {1: Fraction(0), 2: Fraction(250)}
        assert unpresented == [0, 3]

    def test_unpresented_has_reason(self, generated_dir):
        tl = _timeline(generated_dir, "trim_edit.mp4")
        for u in tl.unpresented:
            assert u.reason  # 不静默丢弃


class TestMultiEdit:
    def test_two_segments_with_gap(self, generated_dir):
        tl = _timeline(generated_dir, "multi_edit.mp4")
        presented = {p.sample_index: p.movie_time for p in tl.presentations}
        assert presented == {0: Fraction(0), 2: Fraction(250)}
        assert [u.sample_index for u in tl.unpresented] == [1, 3]


class TestImplicitEdit:
    def test_no_elst_means_identity(self, generated_dir):
        """无 edts/elst 时按恒等编辑：movie_time == PTS 换算值。"""

        # bframes 有 elst；构造一个无 elst 的最小验证用 multitrack 的音轨？
        # multitrack 两轨都有 elst。改用直接构建 Track 的方式。
        from mp4timeline.mp4parse.parser import Track
        from mp4timeline.mp4parse.sample_table import Sample

        track = Track(
            track_id=9,
            handler="vide",
            media_timescale=1000,
            media_duration=300,
            samples=tuple(
                Sample(i, i * 100, 0, 100, 0, 10, True) for i in range(3)
            ),
            edits=(),
        )
        tl = build_track_timeline(track, 1000)
        assert [p.movie_time for p in tl.presentations] == [
            Fraction(0),
            Fraction(100),
            Fraction(200),
        ]
