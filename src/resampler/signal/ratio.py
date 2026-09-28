"""Rational ratio arithmetic.

The resampling factor is always expressed as coprime integers L/M:

    f_out / f_in = L / M

with the time relation for output sample ``n`` (see signal/engine.py):

    t_out(n) = (n*M - delay_high) / (L * f_in)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..errors import InputValidationError, ResourceExhaustedError


@dataclass(frozen=True)
class RationalRatio:
    up: int            # L
    down: int          # M
    rate_in: float
    rate_out: float


def reduce_ratio(rate_in: float | int, rate_out: float | int,
                 max_term: int) -> RationalRatio:
    """Validate sample rates and reduce L/M to coprime integers.

    Rates are accepted as positive numbers. Integer-valued inputs
    (e.g. 48000, 44100) are reduced exactly via gcd; other positive
    values are rejected: the service contract is an *exact* rational
    ratio, not an approximate float factor.
    """
    def _as_pos_int(value, name: str) -> int:
        if isinstance(value, bool):
            raise InputValidationError(f"{name} must be a positive integer",
                                       {"got": value})
        if isinstance(value, int):
            iv = value
        elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
            iv = int(value)
        else:
            raise InputValidationError(
                f"{name} must be an integer sample rate (exact rational ratio)",
                {"got": value})
        if iv <= 0:
            raise InputValidationError(f"{name} must be positive", {"got": value})
        return iv

    fi = _as_pos_int(rate_in, "input_rate")
    fo = _as_pos_int(rate_out, "output_rate")
    g = math.gcd(fi, fo)
    L, M = fo // g, fi // g
    if max(L, M) > max_term:
        raise ResourceExhaustedError(
            "reduced ratio term exceeds configured limit",
            {"up": L, "down": M, "limit": max_term})
    return RationalRatio(up=L, down=M, rate_in=float(fi), rate_out=float(fo))
