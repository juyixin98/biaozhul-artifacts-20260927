"""Parsing of local segment descriptors (synthetic fixtures).

A descriptor is a JSON document describing one media file as a list of
streams, each with codec parameters, a rational time base and a sample
table in decode order.  The parser validates the schema strictly and
hashes the raw file so plans and logs can be tied to exact inputs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.errors import FailureCategory, PlannerError
from app.models import Sample, Segment, Stream

_SAMPLE_REQUIRED = ("dts", "pts", "duration")
_STREAM_REQUIRED = ("type", "codec", "time_base", "samples")


def _fail(detail: str, **context: Any) -> PlannerError:
    return PlannerError(FailureCategory.INPUT_ERROR, detail, context)


def _parse_sample(raw: dict[str, Any], index: int, where: str) -> Sample:
    for key in _SAMPLE_REQUIRED:
        if key not in raw:
            raise _fail(f"sample {index} of {where} misses '{key}'", sample=raw)
    dts, pts, dur = raw["dts"], raw["pts"], raw["duration"]
    if not all(isinstance(v, int) for v in (dts, pts, dur)):
        raise _fail(f"sample {index} of {where} has non-integer timestamps")
    if dur <= 0:
        raise _fail(f"sample {index} of {where} has non-positive duration {dur}")
    deps = raw.get("depends_on", [])
    if not all(isinstance(d, int) and d >= 0 for d in deps):
        raise _fail(f"sample {index} of {where} has invalid depends_on {deps}")
    return Sample(
        index=index,
        dts=dts,
        pts=pts,
        duration=dur,
        keyframe=bool(raw.get("keyframe", False)),
        idr=bool(raw.get("idr", False)),
        depends_on=tuple(deps),
    )


def _parse_stream(raw: dict[str, Any], where: str) -> Stream:
    for key in _STREAM_REQUIRED:
        if key not in raw:
            raise _fail(f"stream of {where} misses '{key}'", stream=raw.get("type"))
    stream_type = raw["type"]
    if stream_type not in ("video", "audio"):
        raise _fail(f"unsupported stream type {stream_type!r} in {where}")
    tb = raw["time_base"]
    if (not isinstance(tb, list) or len(tb) != 2
            or not all(isinstance(v, int) and v > 0 for v in tb)):
        raise _fail(f"invalid time_base {tb!r} in {where}")
    samples = tuple(
        _parse_sample(s, i, where) for i, s in enumerate(raw["samples"]))
    # sample-level invariants of a well-formed descriptor
    for smp in samples:
        for dep in smp.depends_on:
            if dep >= len(samples):
                # kept as a descriptor: the *planner* must surface this as
                # MISSING_REFERENCE, so parsing accepts it.
                continue
    return Stream(
        stream_type=stream_type,
        codec=raw["codec"],
        time_base=(tb[0], tb[1]),
        samples=samples,
        profile=raw.get("profile"),
        level=raw.get("level"),
        width=raw.get("width"),
        height=raw.get("height"),
        pix_fmt=raw.get("pix_fmt"),
        sample_rate=raw.get("sample_rate"),
        channels=raw.get("channels"),
        encoder_delay=int(raw.get("encoder_delay", 0)),
    )


def load_segment(path: str | Path) -> Segment:
    """Load and validate one segment descriptor from disk."""
    p = Path(path)
    if not p.is_file():
        raise _fail(f"segment descriptor not found: {p}")
    raw_bytes = p.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        doc = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise _fail(f"invalid JSON in {p}: {exc}")
    if not isinstance(doc, dict) or "streams" not in doc:
        raise _fail(f"{p}: descriptor must be an object with 'streams'")
    if not doc["streams"]:
        raise _fail(f"{p}: descriptor has no streams")
    streams = tuple(
        _parse_stream(s, p.name) for s in doc["streams"])
    return Segment(
        name=doc.get("name", p.stem),
        container=doc.get("container", "unknown"),
        streams=streams,
        source_path=str(p),
        sha256=digest,
    )
