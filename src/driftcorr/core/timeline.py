"""Metadata time mapping, kept separate from audio resampling.

The same (offset, drift) estimate yields a closed-form mapping between the
two timelines. Applying it to business-event timestamps must not require —
or imply — re-rendering any audio.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeMapping:
    """Affine map between target-clock and reference-clock times."""

    offset_s: float
    drift_ppm: float

    @property
    def drift_ratio(self) -> float:
        return 1.0 + self.drift_ppm * 1e-6

    def target_to_ref(self, t_target_s: float) -> float:
        return (t_target_s - self.offset_s) / self.drift_ratio

    def ref_to_target(self, t_ref_s: float) -> float:
        return self.offset_s + self.drift_ratio * t_ref_s


@dataclass(frozen=True)
class MappedEvent:
    name: str
    target_time_s: float
    ref_time_s: float
    within_usable_interval: bool


def map_events(
    events: list,  # list of media.metadata.TimedEvent (name, time_s)
    mapping: TimeMapping,
    usable_interval_s: tuple[float, float],
) -> list[MappedEvent]:
    """Map target-clock event timestamps onto the reference timeline.

    Events outside the usable interval are still mapped, but flagged — the
    drift model is only validated between the first and last inlier sync
    point, and extrapolation beyond it is reported, not hidden.
    """
    lo, hi = usable_interval_s
    out = []
    for e in events:
        t_ref = mapping.target_to_ref(e.time_s)
        out.append(MappedEvent(
            name=e.name,
            target_time_s=float(e.time_s),
            ref_time_s=float(t_ref),
            within_usable_interval=bool(lo <= t_ref <= hi),
        ))
    return out
