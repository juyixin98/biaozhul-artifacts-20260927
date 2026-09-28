"""Time & signal kernel: sample tables -> DTS/PTS -> edit-list presentation map.

Conventions
-----------
* DTS and PTS are integers in the track's *media* timescale.  PTS = DTS + CTO
  where CTO comes from ctts and keeps its sign (version-1 ctts may be
  negative; version-0 offsets are unsigned by definition).
* All media->movie timescale conversion is exact rational arithmetic
  (``fractions.Fraction``); no float rounding anywhere in the kernel.
* Edit list semantics (ISO 14496-12, restricted):
    - entries are applied in order along the movie timeline; the movie cursor
      advances by ``segment_duration`` (movie timescale units) per entry;
    - ``media_time == -1`` is an *empty edit*: it occupies movie time and
      maps to no media samples (recorded as a gap);
    - a normal edit presents every sample whose PTS falls in the half-open
      media window ``[media_time, media_time + seg*media_ts/movie_ts)``;
      the sample's movie start is ``cursor + (pts - media_time)`` converted
      to movie units; its movie end is clipped at the edit's movie end;
    - only edit rate 1.0 is supported; anything else raises
      :class:`UnsupportedFeatureError`;
    - a track without elst gets one implicit edit covering the whole mdhd
      duration from media time 0.
* A sample's presentation duration is its stts decode delta mapped to movie
  time (documented assumption; B-frame tails may be clipped by the edit end).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from .boxes import EditEntry, MovieBoxes, TrackBoxes
from .errors import MalformedBoxError, TableConsistencyError, UnsupportedFeatureError


@dataclass(frozen=True)
class Sample:
    index: int
    dts: int            # media timescale
    cto: int            # signed composition offset, media timescale
    pts: int            # dts + cto, media timescale
    duration: int       # stts decode delta, media timescale
    byte_offset: int    # absolute file offset of the sample's first byte
    byte_size: int
    is_sync: bool


@dataclass(frozen=True)
class Presentation:
    sample_index: int
    movie_start: Fraction   # movie timescale units
    movie_end: Fraction


@dataclass(frozen=True)
class Gap:
    movie_start: Fraction
    movie_end: Fraction


@dataclass
class TrackTimeline:
    track_id: int
    handler: str
    media_timescale: int
    samples: list[Sample]
    edits: list[EditEntry]
    presentations: list[Presentation]
    gaps: list[Gap]
    movie_duration: Fraction


@dataclass
class MovieTimeline:
    movie_timescale: int
    brands: list[str]
    tracks: list[TrackTimeline] = field(default_factory=list)


def to_movie_time(value: int | Fraction, media_timescale: int, movie_timescale: int) -> Fraction:
    """Exact rational conversion of a media-time value into movie-time units."""
    return Fraction(value) * movie_timescale / media_timescale


def build_samples(track: TrackBoxes, file_size: int) -> list[Sample]:
    """Expand stts/ctts/stsc/stsz/stco into a per-sample table (NumPy)."""
    n_from_stts = int(track.stts_counts.sum())
    n = int(track.stsz_sizes.shape[0])
    if n_from_stts != n:
        raise TableConsistencyError(
            f"track {track.track_id}: stts covers {n_from_stts} samples but stsz "
            f"declares {n}"
        )
    if n == 0:
        raise TableConsistencyError(f"track {track.track_id}: zero samples")

    # --- decode timestamps -------------------------------------------------
    deltas = np.repeat(track.stts_deltas, track.stts_counts)
    dts = np.zeros(n, dtype=np.int64)
    dts[1:] = np.cumsum(deltas)[:-1]

    # --- composition offsets (sign preserved) ------------------------------
    if track.ctts_counts is not None:
        if int(track.ctts_counts.sum()) != n:
            raise TableConsistencyError(
                f"track {track.track_id}: ctts covers {int(track.ctts_counts.sum())} "
                f"samples, expected {n}"
            )
        cto = np.repeat(track.ctts_offsets, track.ctts_counts)
    else:
        cto = np.zeros(n, dtype=np.int64)
    pts = dts + cto

    # --- sample -> chunk mapping -> byte offsets ---------------------------
    first_chunk = track.stsc_first_chunk
    if first_chunk[0] != 1 or np.any(np.diff(first_chunk) <= 0):
        raise TableConsistencyError(
            f"track {track.track_id}: stsc first_chunk values must start at 1 and increase"
        )
    n_chunks = int(track.chunk_offsets.shape[0])
    if first_chunk[-1] > n_chunks:
        raise TableConsistencyError(
            f"track {track.track_id}: stsc references chunk {first_chunk[-1]} but only "
            f"{n_chunks} chunk offsets exist"
        )
    chunk_ids = np.arange(1, n_chunks + 1)
    spc = track.stsc_samples_per_chunk[np.searchsorted(first_chunk, chunk_ids, side="right") - 1]
    if int(spc.sum()) != n:
        raise TableConsistencyError(
            f"track {track.track_id}: stsc/stco layout covers {int(spc.sum())} samples, "
            f"stsz declares {n}"
        )

    sizes = track.stsz_sizes
    offsets = np.empty(n, dtype=np.int64)
    pos = 0
    for chunk_idx in range(n_chunks):
        k = int(spc[chunk_idx])
        chunk_sizes = sizes[pos: pos + k]
        offsets[pos: pos + k] = track.chunk_offsets[chunk_idx] + np.concatenate(
            ([0], np.cumsum(chunk_sizes)[:-1])
        )
        pos += k

    last_end = offsets + sizes
    if offsets.min() < 0 or int(last_end.max()) > file_size:
        raise TableConsistencyError(
            f"track {track.track_id}: sample byte range exceeds file size {file_size}"
        )

    # --- sync flags ---------------------------------------------------------
    if track.stss is None:
        sync = np.ones(n, dtype=bool)
    else:
        sync = np.zeros(n, dtype=bool)
        sync[track.stss - 1] = True

    return [
        Sample(
            index=i,
            dts=int(dts[i]),
            cto=int(cto[i]),
            pts=int(pts[i]),
            duration=int(deltas[i]),
            byte_offset=int(offsets[i]),
            byte_size=int(sizes[i]),
            is_sync=bool(sync[i]),
        )
        for i in range(n)
    ]


def apply_edit_list(
    samples: list[Sample],
    edits: list[EditEntry],
    media_timescale: int,
    movie_timescale: int,
    media_duration: int,
) -> tuple[list[Presentation], list[Gap], Fraction]:
    """Map media samples onto the movie timeline.  Returns (presentations,
    gaps, movie_duration) with exact rational movie times."""
    if not edits:
        # Implicit edit: whole mdhd duration from media time 0.  The segment
        # duration is a Fraction of movie-timescale units (may be non-integer).
        edits = [
            EditEntry(
                segment_duration=to_movie_time(media_duration, media_timescale, movie_timescale),
                media_time=0,
                rate_integer=1,
                rate_fraction=0,
            )
        ]

    presentations: list[Presentation] = []
    gaps: list[Gap] = []
    by_pts = sorted(samples, key=lambda s: (s.pts, s.index))
    cursor = Fraction(0)  # movie-time cursor, in movie timescale units

    for entry in edits:
        seg = Fraction(entry.segment_duration)
        if seg <= 0:
            raise MalformedBoxError(
                f"edit with non-positive segment_duration {entry.segment_duration}"
            )
        if entry.media_time == -1:
            gaps.append(Gap(cursor, cursor + seg))  # empty edit: movie time, no media
            cursor += seg
            continue
        if entry.rate_integer != 1 or entry.rate_fraction != 0:
            raise UnsupportedFeatureError(
                f"edit rate {entry.rate_integer}.{entry.rate_fraction:05d} unsupported; "
                "only rate 1.0"
            )
        window_start = Fraction(entry.media_time)                      # media units
        window_end = window_start + seg * media_timescale / movie_timescale
        for s in by_pts:
            if window_start <= s.pts < window_end:
                start = cursor + to_movie_time(
                    s.pts - entry.media_time, media_timescale, movie_timescale
                )
                end = start + to_movie_time(s.duration, media_timescale, movie_timescale)
                presentations.append(
                    Presentation(s.index, start, min(end, cursor + seg))
                )
        cursor += seg

    presentations.sort(key=lambda p: (p.movie_start, p.sample_index))
    return presentations, gaps, cursor


def build_track_timeline(track: TrackBoxes, movie_timescale: int, file_size: int) -> TrackTimeline:
    samples = build_samples(track, file_size)
    presentations, gaps, movie_duration = apply_edit_list(
        samples, track.edits, track.media_timescale, movie_timescale, track.media_duration
    )
    return TrackTimeline(
        track_id=track.track_id,
        handler=track.handler,
        media_timescale=track.media_timescale,
        samples=samples,
        edits=track.edits,
        presentations=presentations,
        gaps=gaps,
        movie_duration=movie_duration,
    )


def build_movie_timeline(movie: MovieBoxes) -> MovieTimeline:
    tl = MovieTimeline(movie_timescale=movie.movie_timescale, brands=movie.brands)
    for track in movie.tracks:
        tl.tracks.append(build_track_timeline(track, movie.movie_timescale, movie.file_size))
    return tl


# --------------------------------------------------------------------------
# JSON serialization (exact rationals as {num, den}; floats only as a view)
# --------------------------------------------------------------------------

def _frac(f: Fraction) -> dict:
    return {"num": f.numerator, "den": f.denominator}


def track_timeline_to_dict(tl: TrackTimeline, movie_timescale: int) -> dict:
    return {
        "track_id": tl.track_id,
        "handler": tl.handler,
        "media_timescale": tl.media_timescale,
        "movie_duration": _frac(tl.movie_duration),
        "movie_duration_seconds": float(tl.movie_duration) / movie_timescale,
        "samples": [
            {
                "index": s.index,
                "dts": s.dts,
                "cto": s.cto,
                "pts": s.pts,
                "duration": s.duration,
                "byte_offset": s.byte_offset,
                "byte_size": s.byte_size,
                "is_sync": s.is_sync,
            }
            for s in tl.samples
        ],
        "edits": [
            {
                "segment_duration": e.segment_duration,
                "media_time": e.media_time,
                "rate": f"{e.rate_integer}.{e.rate_fraction:05d}",
            }
            for e in tl.edits
        ],
        "gaps": [
            {"movie_start": _frac(g.movie_start), "movie_end": _frac(g.movie_end)}
            for g in tl.gaps
        ],
        "presentations": [
            {
                "sample_index": p.sample_index,
                "movie_start": _frac(p.movie_start),
                "movie_end": _frac(p.movie_end),
            }
            for p in tl.presentations
        ],
    }


def movie_timeline_to_dict(tl: MovieTimeline) -> dict:
    return {
        "movie_timescale": tl.movie_timescale,
        "brands": tl.brands,
        "tracks": [track_timeline_to_dict(t, tl.movie_timescale) for t in tl.tracks],
    }
