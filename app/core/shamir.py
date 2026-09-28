"""Shamir threshold split/recover built on the galois field backend.

This module contains the cryptographic orchestration but **no** integrity or
parsing policy -- those live in :mod:`app.core.envelope` and
:mod:`app.parsing` respectively, so the three concerns stay independently
testable.
"""
from __future__ import annotations

import os
import dataclasses
import enum

from .field import CHUNK_BYTES, FieldParams, SECP256K1_P, lagrange_constant, poly_eval, verify_points_fit

_BLOCK = CHUNK_BYTES + 1  # 32-byte field elements carry 31 secret bytes


class RecoverStatus(str, enum.Enum):
    """Outcome of a recovery attempt (the *failure category*, not just ok/no)."""

    RECOVERED_VERIFIED = "recovered_verified"
    # Exactly the threshold of MAC-valid, compatible shares: mathematically
    # recovered, but there is no redundancy to cross-check.
    RECOVERED_UNVERIFIABLE = "recovered_unverifiable"
    REJECTED_INSUFFICIENT = "rejected_insufficient_threshold"
    # MAC-valid shares disagree with one another (a valid-shareholder/key-trust
    # problem) -- see the README trust boundary.
    REJECTED_INCONSISTENT = "rejected_inconsistent_shares"


class SplitError(ValueError):
    """Invalid split parameters."""


@dataclasses.dataclass
class SplitResult:
    # Ordered by x = 1..total; each inner tuple aligns by secret block.
    shares: list[tuple[int, list[int]]]


@dataclasses.dataclass
class RecoverResult:
    status: RecoverStatus
    secret: bytes | None
    # The de-duplicated set of (x, fingerprint-agnostic) points actually used
    # or inspected, so callers can build precise diagnostics.
    used_xs: list[int]
    extra_xs: list[int] = dataclasses.field(default_factory=list)
    # When the set is inconsistent, the xs detected *not* on the baseline poly.
    # Detection only -- this is not proof these are the malicious parties.
    mismatched_xs: list[int] = dataclasses.field(default_factory=list)
    detail: str = ""


# --------------------------------------------------------------------------- #
# Secret <-> field-element block encoding
# --------------------------------------------------------------------------- #
def encode_secret(secret: bytes) -> list[int]:
    """Split a byte secret into 31-byte blocks, each encoded as one element.

    Layout per block (32 bytes): ``[0x00 padding][1-byte original length]``?
    No -- we keep the length byte as the *first* byte and pad the remainder:
    ``[len(1 byte)][chunk (len bytes)][zero padding]``. This is unambiguous for
    every block including the final/only block, and every encoded value has at
    least one leading zero byte so it is strictly less than the 256-bit prime.
    """
    if not isinstance(secret, (bytes, bytearray)):
        raise TypeError("secret must be bytes")
    blocks: list[int] = []
    if len(secret) == 0:
        # Represent the empty secret as a single zero-length block.
        return [0]
    for start in range(0, len(secret), CHUNK_BYTES):
        chunk = bytes(secret[start : start + CHUNK_BYTES])
        padded = bytes([len(chunk)]) + chunk
        padded = padded.ljust(_BLOCK, b"\x00")
        value = int.from_bytes(padded, "big")
        if value >= SECP256K1_P:  # pragma: no cover - impossible by construction
            raise ValueError("encoded block exceeds field prime")
        blocks.append(value)
    return blocks


def decode_secret(blocks: list[int]) -> bytes:
    out = bytearray()
    for value in blocks:
        if not 0 <= value < SECP256K1_P:
            raise ValueError("decoded block outside field")
        raw = value.to_bytes(_BLOCK, "big")
        length = raw[0]
        if length > CHUNK_BYTES:
            raise ValueError("corrupt block length prefix")
        # Content sits immediately after the length byte.
        out.extend(raw[1 : 1 + length])
    return bytes(out)


# --------------------------------------------------------------------------- #
# Split
# --------------------------------------------------------------------------- #
def _random_nonzero() -> int:
    # Uniform in [1, p-1]
    while True:
        candidate = int.from_bytes(os.urandom(32), "big")
        if 0 < candidate < SECP256K1_P:
            return candidate


def split_secret(secret: bytes, threshold: int, total: int) -> SplitResult:
    if not (1 <= threshold <= total):
        raise SplitError("require 1 <= threshold <= total")
    if total > 4096:
        raise SplitError("total too large")

    blocks = encode_secret(secret)
    # One polynomial per secret block; coefficients in descending degree with
    # the constant term equal to the secret element.
    polys: list[list[int]] = []
    for element in blocks:
        # degree = threshold - 1 => threshold coefficients
        coeffs = [_random_nonzero() for _ in range(threshold - 1)]
        coeffs.append(element)  # descending order: constant last
        polys.append(coeffs)

    shares: list[tuple[int, list[int]]] = []
    for x in range(1, total + 1):
        ys = [poly_eval(poly, x) for poly in polys]
        shares.append((x, ys))
    return SplitResult(shares=shares)


# --------------------------------------------------------------------------- #
# Recover (math only -- input is assumed MAC-valid & field-compatible by now)
# --------------------------------------------------------------------------- #
def recover_from_points(
    points_by_x: dict[int, list[int]],
    threshold: int,
    block_count: int,
) -> RecoverResult:
    """``points_by_x`` maps a *de-duplicated* x to its per-block ys."""
    xs = sorted(points_by_x)
    if len(xs) < threshold:
        return RecoverResult(
            status=RecoverStatus.REJECTED_INSUFFICIENT,
            secret=None,
            used_xs=xs,
            detail=(
                f"only {len(xs)} distinct-share point(s), threshold is {threshold}"
            ),
        )

    baseline = xs[:threshold]
    extra = xs[threshold:]

    # Interpolate each block using the baseline subset.
    blocks: list[int] = []
    for block in range(block_count):
        block_ys = [points_by_x[x][block] for x in baseline]
        blocks.append(lagrange_constant(baseline, block_ys))

    if not extra:
        try:
            secret = decode_secret(blocks)
        except ValueError as exc:
            # A bogus constant term can still decode-fail; with no redundancy we
            # cannot tell which share caused it.
            return RecoverResult(
                status=RecoverStatus.REJECTED_INCONSISTENT,
                secret=None,
                used_xs=baseline,
                detail=f"reconstructed value failed decoding: {exc}",
            )
        return RecoverResult(
            status=RecoverStatus.RECOVERED_UNVERIFIABLE,
            secret=secret,
            used_xs=baseline,
            detail="recovered with exactly the threshold; no redundancy to verify",
        )

    # Cross-check every extra share against the baseline polynomial, per block.
    mismatched: set[int] = set()
    for block in range(block_count):
        block_ys = [points_by_x[x][block] for x in baseline]
        check_points = [(x, points_by_x[x][block]) for x in extra]
        for bad_x in verify_points_fit(check_points, baseline, block_ys):
            mismatched.add(bad_x)

    if mismatched:
        return RecoverResult(
            status=RecoverStatus.REJECTED_INCONSISTENT,
            secret=None,
            used_xs=baseline,
            extra_xs=extra,
            mismatched_xs=sorted(mismatched),
            detail=(
                "MAC-valid shares disagree on the underlying polynomial; "
                "mismatch detected but cannot be attributed to a specific party"
            ),
        )

    try:
        secret = decode_secret(blocks)
    except ValueError as exc:
        return RecoverResult(
            status=RecoverStatus.REJECTED_INCONSISTENT,
            secret=None,
            used_xs=baseline,
            extra_xs=extra,
            detail=f"reconstructed value failed decoding: {exc}",
        )
    return RecoverResult(
        status=RecoverStatus.RECOVERED_VERIFIED,
        secret=secret,
        used_xs=baseline,
        extra_xs=extra,
        detail=f"recovered and verified against {len(extra)} extra share(s)",
    )


__all__ = [
    "RecoverStatus",
    "SplitError",
    "SplitResult",
    "RecoverResult",
    "encode_secret",
    "decode_secret",
    "split_secret",
    "recover_from_points",
]
