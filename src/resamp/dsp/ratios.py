"""Rational sample-rate ratio reduction and validation."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..errors import InvalidInputError, ResourceExhaustedError


@dataclass(frozen=True)
class RationalRatio:
    """Coprime ratio ``fout / fin = L / M`` (L upsample, M downsample)."""

    fin: int
    fout: int
    l: int
    m: int

    @property
    def high_rate(self) -> int:
        return self.l * self.fin  # == self.m * self.fout

    @classmethod
    def reduce(cls, fin: int, fout: int, *, max_rate: int = 10_000_000,
              max_factor: int = 4096) -> "RationalRatio":
        if not isinstance(fin, int) or not isinstance(fout, int) \
                or isinstance(fin, bool) or isinstance(fout, bool):
            raise InvalidInputError(
                "sample rates must be integers",
                details={"fin": repr(fin), "fout": repr(fout)},
            )
        if fin <= 0 or fout <= 0:
            raise InvalidInputError(
                "sample rates must be positive",
                details={"fin": fin, "fout": fout},
            )
        if fin > max_rate or fout > max_rate:
            raise InvalidInputError(
                f"sample rate exceeds configured maximum {max_rate}",
                details={"fin": fin, "fout": fout, "max_rate": max_rate},
            )
        g = math.gcd(fin, fout)
        l, m = fout // g, fin // g
        if max(l, m) > max_factor:
            raise ResourceExhaustedError(
                f"reduced ratio factor too large: L={l}, M={m} "
                f"(limit {max_factor}); choose rates with a larger common divisor",
                details={"fin": fin, "fout": fout, "l": l, "m": m,
                         "max_factor": max_factor},
            )
        return cls(fin=fin, fout=fout, l=l, m=m)
