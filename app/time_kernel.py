"""Time and signal kernel: modular RTP arithmetic and clock-drift modelling.

This module contains *no* buffering policy. It only knows how RTP sequence
numbers and timestamps behave as finite-width counters, and how to convert
between sender clock units and receiver (wall) milliseconds. Keeping this
pure lets the tests pin the arithmetic independently of policy.
"""
from __future__ import annotations

from dataclasses import dataclass


def wrap_delta(value: int, reference: int, bits: int) -> int:
    """Signed modular distance ``value - reference`` for a counter of ``bits``.

    Works correctly across a single wraparound. A returned delta whose
    magnitude exceeds half the counter space means the two values are more
    than half a cycle apart (treated by callers as a new/reset stream).
    """
    mod = 1 << bits
    half = mod >> 1
    delta = (value - reference) % mod
    if delta >= half:
        delta -= mod
    return delta


def seq_forward_distance(ahead: int, behind: int, bits: int = 16) -> int:
    """How many sequence steps ``ahead`` is after ``behind`` (>=0 expected)."""
    d = wrap_delta(ahead, behind, bits)
    return d


def ts_to_ms(ticks: int, clock_rate: int) -> float:
    return 1000.0 * ticks / clock_rate


def ms_to_ticks(ms: float, clock_rate: int) -> float:
    return ms * clock_rate / 1000.0


@dataclass
class DriftModel:
    """Linear mapping from sender RTP time to receiver wall-clock ms.

    wall_ms = (sender_ms - anchor_sender_ms) / skew + anchor_wall_ms

    ``skew`` is sender-seconds per receiver-second: skew < 1 means the sender
    clock runs slow relative to the receiver, > 1 means fast. A fixed skew is
    sufficient for the offline fixtures; the jitter estimator reacts to the
    resulting transit-time trend.
    """

    anchor_sender_ms: float
    anchor_wall_ms: float
    skew: float = 1.0

    def to_wall_ms(self, sender_ms: float) -> float:
        return self.anchor_wall_ms + (sender_ms - self.anchor_sender_ms) / self.skew
