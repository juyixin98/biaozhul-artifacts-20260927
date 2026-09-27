"""多 timescale 有理数映射测试：视频 90000 / 音频 48000 / 电影 1000。"""

from fractions import Fraction

from mp4timeline.mp4parse.parser import parse_movie
from mp4timeline.timeline.kernel import build_track_timeline


def test_video_fractional_movie_times(generated_dir, testlog):
    movie = parse_movie((generated_dir / "multitrack.mp4").read_bytes(), "multitrack.mp4")
    video = movie.tracks[0]
    assert video.media_timescale == 90000
    tl = build_track_timeline(video, movie.movie_timescale)
    actual = [(p.sample_index, str(p.movie_time), str(p.movie_duration)) for p in tl.presentations]
    expected = [
        (0, "0", "100/3"),
        (1, "100/3", "100/3"),
        (2, "200/3", "100/3"),
    ]
    testlog.write(
        {
            "event": "assertion",
            "test": "multitrack_video_fractional",
            "basis": "手写参考 multitrack.json：3000/90000*1000=100/3 ms，全程 Fraction 精确",
            "expected": expected,
            "actual": actual,
        }
    )
    assert actual == expected


def test_audio_integer_movie_times(generated_dir):
    movie = parse_movie((generated_dir / "multitrack.mp4").read_bytes(), "multitrack.mp4")
    audio = movie.tracks[1]
    assert audio.media_timescale == 48000
    tl = build_track_timeline(audio, movie.movie_timescale)
    actual = [str(p.movie_time) for p in tl.presentations]
    assert actual == ["0", "20", "40", "60", "80"]
    assert all(p.movie_duration == Fraction(20) for p in tl.presentations)


def test_playback_intervals_in_seconds(generated_dir):
    movie = parse_movie((generated_dir / "multitrack.mp4").read_bytes(), "multitrack.mp4")
    tl = build_track_timeline(movie.tracks[0], movie.movie_timescale)
    intervals = []
    for p in tl.presentations:
        intervals.append(
            [
                p.movie_time / Fraction(movie.movie_timescale),
                (p.movie_time + p.movie_duration) / Fraction(movie.movie_timescale),
            ]
        )
    assert intervals[0] == [Fraction(0), Fraction(1, 30)]
    assert intervals[1] == [Fraction(1, 30), Fraction(1, 15)]
    assert intervals[2] == [Fraction(1, 15), Fraction(1, 10)]


def test_video_audio_aligned_at_end(generated_dir):
    """两轨都映射到 100ms：视频 3*(100/3)=100，音频 5*20=100。"""

    movie = parse_movie((generated_dir / "multitrack.mp4").read_bytes(), "multitrack.mp4")
    ends = []
    for track in movie.tracks:
        tl = build_track_timeline(track, movie.movie_timescale)
        ends.append(tl.movie_span[1])
    assert ends == [Fraction(100), Fraction(100)]
