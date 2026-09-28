"""Finite-field backend.

We use the **galois** library as the mature finite-field implementation rather
than hand-rolling modular arithmetic in production code. All field operations
(polynomial evaluation, interpolation, inverses) are performed on galois field
arrays over a fixed, named prime field.

Field choice
------------
``p = 2**256 - 2**32 - 977`` -- the well-known secp256k1 base-field prime. It
is large enough that a whole 31-byte block fits as a single field element with
room to spare, and it is a fixed, documented constant, so every share in a
collection is bound to the same field. Shares produced over a different prime
are rejected at parse time (see :mod:`app.parsing`).
"""
from __future__ import annotations

import dataclasses
import functools

# secp256k1 base-field prime: 2^256 - 2^32 - 977
SECP256K1_P = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
)

# How many *secret* bytes we encode per field element. Each chunk is length
# prefixed with one byte, so 32-byte elements hold 31 secret bytes. We use 31
# rather than 32 so the encoded integer is provably < p for every block, which
# keeps the guarantee simple and auditable.
CHUNK_BYTES = 31

FIELD_VERSION = "gf-secp256k1-v1"


@dataclasses.dataclass(frozen=True)
class FieldParams:
    """The field parameters a share is bound to (part of share identity)."""

    version: str = FIELD_VERSION
    prime: int = SECP256K1_P
    prime_bits: int = 256
    chunk_bytes: int = CHUNK_BYTES

    def to_dict(self) -> dict[str, object]:
        # ``prime`` is serialised as a decimal string to survive JSON (no
        # arbitrary-precision integer assumption in downstream consumers).
        return {
            "version": self.version,
            "prime": str(self.prime),
            "prime_bits": self.prime_bits,
            "chunk_bytes": self.chunk_bytes,
        }

    @staticmethod
    def from_dict(raw: dict[str, object]) -> "FieldParams":
        fp = FieldParams()
        if str(raw.get("version")) != fp.version:
            raise FieldMismatch(
                f"field version mismatch: got {raw.get('version')!r}, "
                f"want {fp.version!r}"
            )
        if str(raw.get("prime")) != str(fp.prime):
            raise FieldMismatch("share was produced over a different prime")
        if int(raw.get("prime_bits", -1)) != fp.prime_bits:
            raise FieldMismatch("field prime_bits mismatch")
        if int(raw.get("chunk_bytes", -1)) != fp.chunk_bytes:
            raise FieldMismatch("field chunk_bytes mismatch")
        return fp


class FieldMismatch(ValueError):
    """Raised when a share's bound field parameters are not compatible."""


# A known primitive element (generator) of the field. For the secp256k1 prime,
# g=3 is a documented generator. Passing it explicitly lets galois skip its
# (expensive) primitive-element search; ``verify=False`` skips re-running
# Miller-Rabin primality and primitive-root proofs on a constant we pin here.
PRIMITIVE_ELEMENT = 3


@functools.lru_cache(maxsize=1)
def galois_field():
    """Return (and cache) the galois GF(p) class.

    We pin a well-known prime and generator and disable galois's expensive
    build-time verification so constructing the field is sub-millisecond rather
    than ~2 minutes (galois otherwise re-proves primality and searches for a
    primitive element). Correctness of the constant is covered by tests. galois
    is imported lazily so merely importing this module does not require numpy;
    callers that actually do field arithmetic get a clear error if it is absent.
    """
    try:
        import galois  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only when uninstalled
        raise RuntimeError(
            "the 'galois' finite-field library is required for field "
            "arithmetic; install pinned dependencies (see requirements.txt)"
        ) from exc

    return galois.GF(
        SECP256K1_P, primitive_element=PRIMITIVE_ELEMENT, verify=False
    )


def poly_eval(coefficients: list[int], x: int) -> int:
    """Evaluate a polynomial at ``x`` using galois field arithmetic.

    ``coefficients`` are in *descending* degree order (constant last), which is
    the natural order for Horner's rule.
    """
    GF = galois_field()
    acc = GF(0)
    gx = GF(x % SECP256K1_P)
    for coeff in coefficients:
        acc = acc * gx + GF(coeff % SECP256K1_P)
    return int(acc)


def lagrange_constant(xs: list[int], ys: list[int]) -> int:
    """Return f(0) via Lagrange interpolation over GF(p), using galois.

    ``xs`` are assumed already de-duplicated and non-zero. All arithmetic runs
    in the galois field; the returned integer is the reconstructed constant
    term (the secret element).
    """
    if len(xs) != len(ys) or not xs:
        raise ValueError("lagrange_constant requires equal, non-empty points")

    GF = galois_field()
    gxs = GF([x % SECP256K1_P for x in xs])
    gys = GF([y % SECP256K1_P for y in ys])
    zero = GF(0)
    total = GF(0)

    for i in range(len(xs)):
        # Lagrange basis polynomial L_i(0) = prod_{j!=i} (0 - x_j)/(x_i - x_j)
        num = GF(1)
        den = GF(1)
        for j in range(len(xs)):
            if i == j:
                continue
            num = num * (zero - gxs[j])
            den = den * (gxs[i] - gxs[j])
        total = total + gys[i] * (num / den)

    return int(total)


def verify_points_fit(points: list[tuple[int, int]], xs: list[int],
                      ys: list[int]) -> list[int]:
    """Return the x-coordinates among ``points`` NOT on the interpolating poly.

    Used to *detect* (never to silently repair) a share that does not agree with
    the polynomial implied by a threshold subset. Detection != attribution: a
    reported mismatch proves the set is inconsistent, not which specific party
    is malicious (see README trust-boundary discussion).
    """
    GF = galois_field()
    gxs = GF([x % SECP256K1_P for x in xs])
    gys = GF([y % SECP256K1_P for y in ys])
    bad: list[int] = []
    for x, y in points:
        gx = GF(x % SECP256K1_P)
        expected = GF(0)
        for i in range(len(xs)):
            num = GF(1)
            den = GF(1)
            for j in range(len(xs)):
                if i == j:
                    continue
                num = num * (gx - gxs[j])
                den = den * (gxs[i] - gxs[j])
            expected = expected + gys[i] * (num / den)
        if int(expected) != (y % SECP256K1_P):
            bad.append(x)
    return bad
