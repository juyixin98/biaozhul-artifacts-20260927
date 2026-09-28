"""Exact rational time-base arithmetic.

All timestamp maths is done with integer ticks and Fraction time bases so
that relocation is lossless; a conversion that is not exactly representable
is an input error, never a silent rounding.
"""
from __future__ import annotations

from fractions import Fraction

from app.errors import FailureCategory, PlannerError


def to_seconds(ticks: int, time_base: Fraction) -> Fraction:
    return Fraction(ticks) * time_base


def to_ticks(seconds: Fraction, time_base: Fraction, *, what: str = "value") -> int:
    """Convert rational seconds to integer ticks; refuse inexact results."""
    ticks = Fraction(seconds) / time_base
    if ticks.denominator != 1:
        raise PlannerError(
            FailureCategory.INPUT_ERROR,
            f"{what} = {seconds}s is not representable in time base {time_base}",
            {"seconds": str(seconds), "time_base": str(time_base)},
        )
    return int(ticks)


def rescale(ticks: int, src: Fraction, dst: Fraction, *, what: str = "value") -> int:
    """Rescale integer ticks between time bases exactly."""
    return to_ticks(to_seconds(ticks, src), dst, what=what)
