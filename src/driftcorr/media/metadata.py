"""Sidecar metadata: JSON files describing sync pulses and timed events.

Reference metadata declares where the sync pulses *should* be on the
reference timeline; target metadata may carry business events whose
timestamps need mapping onto the reference timeline after drift estimation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class MetadataError(Exception):
    """Raised when a metadata sidecar is missing required fields."""


@dataclass(frozen=True)
class PulseSpec:
    times_s: list[float]
    duration_s: float
    f0_hz: float
    f1_hz: float


@dataclass(frozen=True)
class TimedEvent:
    name: str
    time_s: float


@dataclass(frozen=True)
class MediaMetadata:
    pulses: PulseSpec | None = None
    events: list[TimedEvent] = field(default_factory=list)


def load_metadata(path: str | Path) -> MediaMetadata:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError(f"cannot read metadata {path}: {exc}") from exc

    pulses = None
    if "pulses" in raw and raw["pulses"] is not None:
        p = raw["pulses"]
        try:
            pulses = PulseSpec(
                times_s=[float(t) for t in p["times_s"]],
                duration_s=float(p["duration_s"]),
                f0_hz=float(p["f0_hz"]),
                f1_hz=float(p["f1_hz"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MetadataError(f"invalid pulses section in {path}: {exc}") from exc

    events = []
    for e in raw.get("events", []):
        try:
            events.append(TimedEvent(name=str(e["name"]), time_s=float(e["time_s"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise MetadataError(f"invalid event entry in {path}: {exc}") from exc

    return MediaMetadata(pulses=pulses, events=events)
