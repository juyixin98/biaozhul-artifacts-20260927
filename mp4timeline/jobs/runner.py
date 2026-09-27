"""作业运行器：读取文件 -> 解析 -> 构建时间线 -> 序列化结果。

所有异常按类别记录到作业（error_class = 异常类名），绝不把失败吞成成功。
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..errors import Mp4TimelineError
from ..mp4parse.parser import Movie, parse_movie
from ..timeline.kernel import TrackTimeline, build_track_timeline
from ..timeline.rational import format_fraction, to_seconds
from .store import JobStore


def serialize_movie(movie: Movie, timelines: list[TrackTimeline]) -> dict:
    tracks = []
    for track, timeline in zip(movie.tracks, timelines):
        tracks.append(
            {
                "track_id": track.track_id,
                "handler": track.handler,
                "media_timescale": track.media_timescale,
                "media_duration": track.media_duration,
                "edits": [
                    {
                        "segment_duration": e.segment_duration,
                        "media_time": e.media_time,
                        "is_empty": e.is_empty,
                    }
                    for e in track.edits
                ],
                "samples": [
                    {
                        "index": s.index,
                        "dts": s.dts,
                        "cto": s.cto,
                        "pts": s.pts,
                        "duration": s.duration,
                        "byte_range": [s.byte_offset, s.size],
                        "is_sync": s.is_sync,
                    }
                    for s in track.samples
                ],
                "presentations": [
                    timeline.presentation_view(p) for p in timeline.presentations
                ],
                "unpresented": [
                    {
                        "sample_index": u.sample_index,
                        "dts": u.dts,
                        "cto": u.cto,
                        "pts": u.pts,
                        "reason": u.reason,
                    }
                    for u in timeline.unpresented
                ],
                "movie_span": [
                    format_fraction(timeline.movie_span[0]),
                    format_fraction(timeline.movie_span[1]),
                ],
                "movie_span_seconds": [
                    format_fraction(
                        to_seconds(timeline.movie_span[0], timeline.movie_timescale)
                    ),
                    format_fraction(
                        to_seconds(timeline.movie_span[1], timeline.movie_timescale)
                    ),
                ],
            }
        )
    return {
        "source": movie.source,
        "sha256": movie.sha256,
        "file_bytes": movie.file_bytes,
        "movie_timescale": movie.movie_timescale,
        "movie_duration": movie.movie_duration,
        "movie_duration_seconds": format_fraction(
            to_seconds(movie.movie_duration, movie.movie_timescale)
        ),
        "tracks": tracks,
    }


def run_job(store: JobStore, job_id: str, settings: Settings) -> None:
    """执行一个已入队的作业。同步执行，状态迁移全程落库。"""

    row = store.get_or_raise(job_id)
    path = Path(row["input_path"])
    try:
        data = path.read_bytes()
        if len(data) > settings.max_file_bytes:
            raise Mp4TimelineError(
                f"文件 {len(data)} 字节超过上限 {settings.max_file_bytes}"
            )
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        store.start(job_id, input_sha256=sha)
        store.log(job_id, f"读取 {path}：{len(data)} 字节 sha256={sha}")

        movie = parse_movie(data, source=str(path))
        store.set_progress(
            job_id, 0.5, f"解析完成：{len(movie.tracks)} 轨，"
            f"movie_timescale={movie.movie_timescale}"
        )

        timelines = [
            build_track_timeline(t, movie.movie_timescale) for t in movie.tracks
        ]
        for tl in timelines:
            store.log(
                job_id,
                f"轨 {tl.track_id}({tl.handler})：{len(tl.presentations)} 个呈现样本，"
                f"{len(tl.unpresented)} 个未呈现，"
                f"movie_span={format_fraction(tl.movie_span[0])}.."
                f"{format_fraction(tl.movie_span[1])}",
            )
        store.set_progress(job_id, 0.9, "时间线构建完成")

        store.complete(job_id, serialize_movie(movie, timelines))
    except Mp4TimelineError as exc:
        store.fail(job_id, type(exc).__name__, str(exc))
    except Exception as exc:  # 未预期错误也如实落库，不伪装成功
        store.fail(job_id, f"Unexpected:{type(exc).__name__}", str(exc))
