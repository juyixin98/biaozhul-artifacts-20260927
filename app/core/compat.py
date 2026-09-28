"""Codec-parameter and time-base compatibility between segments.

Direct concatenation copies compressed samples into one container, so
every parameter the decoder sees must be identical across segments.  Any
mismatch is reported as a structured reason demanding transcode — the
planner never silently "direct-concats" incompatible streams.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.errors import FailureCategory
from app.models import Stream

_VIDEO_PARAM_FIELDS = ("profile", "level", "width", "height", "pix_fmt")
_AUDIO_PARAM_FIELDS = ("profile", "sample_rate", "channels", "encoder_delay")


@dataclass(frozen=True)
class Incompatibility:
    category: FailureCategory
    stream_type: str
    field: str
    values: list[Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "stream_type": self.stream_type,
            "field": self.field,
            "values": [str(v) for v in self.values],
            "requirement": "transcode",
        }


def check_stream_compatibility(streams: list[Stream]) -> list[Incompatibility]:
    """Compare same-type streams of all segments pairwise against the first."""
    problems: list[Incompatibility] = []
    if len(streams) < 2:
        return problems
    ref = streams[0]
    param_fields = (_VIDEO_PARAM_FIELDS if ref.stream_type == "video"
                    else _AUDIO_PARAM_FIELDS)
    for other in streams[1:]:
        if other.codec != ref.codec:
            problems.append(Incompatibility(
                FailureCategory.CODEC_MISMATCH, ref.stream_type, "codec",
                [ref.codec, other.codec]))
        if other.time_base != ref.time_base:
            problems.append(Incompatibility(
                FailureCategory.TIMEBASE_MISMATCH, ref.stream_type, "time_base",
                [list(ref.time_base), list(other.time_base)]))
        for field_name in param_fields:
            ref_val = getattr(ref, field_name)
            other_val = getattr(other, field_name)
            if ref_val != other_val:
                problems.append(Incompatibility(
                    FailureCategory.PARAM_MISMATCH, ref.stream_type, field_name,
                    [ref_val, other_val]))
    return problems


def check_segments_compatibility(segments) -> list[Incompatibility]:
    """All video streams must match each other; same for audio."""
    problems: list[Incompatibility] = []
    for stream_type in ("video", "audio"):
        streams = [s.stream(stream_type) for s in segments]
        streams = [s for s in streams if s is not None]
        problems.extend(check_stream_compatibility(streams))
    return problems
