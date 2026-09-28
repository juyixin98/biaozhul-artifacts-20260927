"""Domain models for segment descriptors and concatenation plans.

Descriptors are parsed from local synthetic fixtures (JSON sample tables);
plans are the output of the core and are serialisable to JSON so they can
be stored in SQLite and re-validated independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any


# ---------------------------------------------------------------------------
# Input side: parsed segment descriptors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sample:
    """One compressed sample in decode order."""
    index: int
    dts: int
    pts: int
    duration: int
    keyframe: bool = False
    idr: bool = False
    # decode-order indices this sample depends on (reference pictures /
    # decoder lead-in).  Empty for self-contained samples.
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class Stream:
    stream_type: str  # "video" | "audio"
    codec: str
    time_base: tuple[int, int]  # (num, den) rational, seconds per tick
    samples: tuple[Sample, ...] = ()
    profile: str | None = None
    level: int | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    encoder_delay: int = 0  # audio priming, in stream ticks

    @property
    def tb(self) -> Fraction:
        return Fraction(self.time_base[0], self.time_base[1])

    def sample_by_index(self, index: int) -> Sample:
        return self.samples[index]


@dataclass(frozen=True)
class Segment:
    name: str
    container: str
    streams: tuple[Stream, ...]
    source_path: str = ""
    sha256: str = ""

    def stream(self, stream_type: str) -> Stream | None:
        for s in self.streams:
            if s.stream_type == stream_type:
                return s
        return None

    def content_end_seconds(self) -> Fraction:
        """Defined segment duration: end of the video track if present,
        otherwise the end of the longest audio track."""
        video = self.stream("video")
        streams = (video,) if video is not None else tuple(
            s for s in self.streams if s.stream_type == "audio")
        end = Fraction(0)
        for s in streams:
            for smp in s.samples:
                cand = Fraction(smp.pts + smp.duration) * s.tb
                if cand > end:
                    end = cand
        return end


@dataclass(frozen=True)
class SegmentRequest:
    """One input segment plus an optional presentation-time trim window,
    expressed in rational seconds."""
    path: str
    trim_in: Fraction = Fraction(0)
    trim_out: Fraction | None = None  # None -> segment content end


# ---------------------------------------------------------------------------
# Output side: the concatenation plan
# ---------------------------------------------------------------------------

# sample roles
ROLE_PRESENT = "present"      # decoded and presented inside the window
ROLE_PREROLL = "preroll"      # decode-only reference lead-in (video)
ROLE_PRIMING = "priming"      # audio encoder-delay lead-in
ROLE_PADDING = "padding"      # synthesised silent audio tail frames

DECISION_DIRECT = "direct_concat"
DECISION_TRANSCODE = "transcode_required"


@dataclass
class PlanSample:
    segment: str
    stream_type: str
    src_index: int | None      # None for synthesised padding samples
    src_dts: int | None
    src_pts: int | None
    out_dts: int
    out_pts: int
    duration: int
    keyframe: bool
    idr: bool
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "stream_type": self.stream_type,
            "src_index": self.src_index,
            "src_dts": self.src_dts,
            "src_pts": self.src_pts,
            "out_dts": self.out_dts,
            "out_pts": self.out_pts,
            "duration": self.duration,
            "keyframe": self.keyframe,
            "idr": self.idr,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanSample":
        return cls(**d)


@dataclass
class TrackPlan:
    stream_type: str
    codec: str
    time_base: tuple[int, int]
    edit_list_media_time: int  # audio priming hidden by the container edit list
    samples: list[PlanSample] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_type": self.stream_type,
            "codec": self.codec,
            "time_base": list(self.time_base),
            "edit_list_media_time": self.edit_list_media_time,
            "samples": [s.to_dict() for s in self.samples],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrackPlan":
        return cls(
            stream_type=d["stream_type"],
            codec=d["codec"],
            time_base=tuple(d["time_base"]),
            edit_list_media_time=d["edit_list_media_time"],
            samples=[PlanSample.from_dict(s) for s in d["samples"]],
        )


@dataclass
class SegmentWindow:
    """The resolved per-stream trim window, in source stream ticks."""
    segment: str
    sha256: str
    windows: dict[str, dict[str, int]]  # stream_type -> {in_tick, out_tick}

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "sha256": self.sha256,
            "windows": self.windows,
        }


@dataclass
class ConcatPlan:
    container: str
    decision: str
    reasons: list[dict[str, Any]]
    tracks: list[TrackPlan]
    segment_windows: list[SegmentWindow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "decision": self.decision,
            "reasons": self.reasons,
            "tracks": [t.to_dict() for t in self.tracks],
            "segment_windows": [w.to_dict() for w in self.segment_windows],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConcatPlan":
        return cls(
            container=d["container"],
            decision=d["decision"],
            reasons=d["reasons"],
            tracks=[TrackPlan.from_dict(t) for t in d["tracks"]],
            segment_windows=[SegmentWindow(w["segment"], w["sha256"], w["windows"])
                             for w in d["segment_windows"]],
        )
