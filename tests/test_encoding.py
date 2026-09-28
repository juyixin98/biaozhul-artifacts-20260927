"""Encoding tests with independently derived expectations.

Reference values come from hand-built bit layouts and an independently
written deinterleaver in this file - never from production ``decode``.
Signed mapping expectations are literal zig-zag values. Every legal
coordinate in small domains round-trips exhaustively.
"""
from __future__ import annotations

import itertools

import pytest

from zindex.encoding import (
    DimSpec,
    Interval,
    SchemaSpec,
    combine128,
    decode,
    decompose_box,
    encode,
    from_unsigned,
    split128,
    to_unsigned,
)

# --------------------------------------------------------------------------- #
# Independent reference implementations (production code not reused)
# --------------------------------------------------------------------------- #
def ref_interleave(unsigned_coords, dims):
    """Independent MSB-first interleaving built from explicit bit positions."""
    n = len(dims)
    max_bits = max(dims)
    stream = []
    for level in range(max_bits):
        for i in range(n):
            if dims[i] > level:
                stream.append((unsigned_coords[i] >> (dims[i] - 1 - level)) & 1)
    code = 0
    for bit in stream:
        code = (code << 1) | bit
    return code


def ref_deinterleave(code, dims):
    """Inverse of ref_interleave, written separately from production decode."""
    n = len(dims)
    max_bits = max(dims)
    total = sum(dims)
    out = [0] * n
    pos = 0
    for level in range(max_bits):
        for i in range(n):
            if dims[i] > level:
                bit = (code >> (total - 1 - pos)) & 1
                out[i] = (out[i] << 1) | bit
                pos += 1
    return tuple(out)


def ref_zigzag(v: int) -> int:
    # literal two's-complement style zig-zag on unbounded ints
    return (v << 1) ^ (v >> 63) if v >= 0 else ((-v << 1) - 1)


# --------------------------------------------------------------------------- #
# schema validation
# --------------------------------------------------------------------------- #
def test_schema_rejects_duplicate_dim_names():
    with pytest.raises(ValueError, match="unique"):
        SchemaSpec(dims=(DimSpec("x", 4), DimSpec("x", 4)))


def test_schema_rejects_total_width_over_128():
    dims = tuple(DimSpec(f"d{i}", 17) for i in range(8))  # 136 bits
    with pytest.raises(ValueError, match="MAX_TOTAL_BITS"):
        SchemaSpec(dims=dims)
    # the 128-bit boundary itself is accepted
    SchemaSpec(dims=tuple(DimSpec(f"d{i}", 16) for i in range(8)))


def test_dim_bounds_validated():
    with pytest.raises(ValueError):
        DimSpec("x", 0)
    with pytest.raises(ValueError):
        DimSpec("x", 65, signed=True)
    # unsigned dimensions cannot exceed 63 (raw column is int64)
    with pytest.raises(ValueError):
        DimSpec("x", 64, signed=False)
    # 64-bit signed is legal: raw fits int64, zig-zag fits uint64
    d = DimSpec("x", 64, signed=True)
    assert to_unsigned(-(1 << 63), d) == (1 << 64) - 1
    assert from_unsigned((1 << 64) - 1, d) == -(1 << 63)


# --------------------------------------------------------------------------- #
# signed mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value,expected", [
    (0, 0), (-1, 1), (1, 2), (-2, 3), (2, 4), (-8, 15), (7, 14),
])
def test_signed_zigzag_literal_4bit(value, expected):
    d = DimSpec("x", 4, signed=True)
    assert to_unsigned(value, d) == expected
    assert from_unsigned(expected, d) == value


def test_unsigned_dimension_is_identity():
    d = DimSpec("u", 5, signed=False)
    for v in range(32):
        assert to_unsigned(v, d) == v


def test_out_of_range_coordinates_rejected():
    s4 = DimSpec("x", 4, signed=True)
    with pytest.raises(ValueError):
        to_unsigned(8, s4)
    with pytest.raises(ValueError):
        to_unsigned(-9, s4)
    u3 = DimSpec("u", 3, signed=False)
    with pytest.raises(ValueError):
        to_unsigned(8, u3)
    with pytest.raises(ValueError):
        to_unsigned(-1, u3)
    with pytest.raises(TypeError):
        to_unsigned(True, s4)


# --------------------------------------------------------------------------- #
# interleaving: hand-computed table
# --------------------------------------------------------------------------- #
def test_2x2bit_handcomputed_table():
    s = SchemaSpec(dims=(DimSpec("x", 2, signed=False), DimSpec("y", 2, signed=False)))
    # bit layout x1 y1 x0 y0
    expected = {
        (0, 0): 0b0000, (0, 1): 0b0001, (0, 2): 0b0100, (0, 3): 0b0101,
        (1, 0): 0b0010, (1, 1): 0b0011, (1, 2): 0b0110, (1, 3): 0b0111,
        (2, 0): 0b1000, (2, 1): 0b1001, (2, 2): 0b1100, (2, 3): 0b1101,
        (3, 0): 0b1010, (3, 1): 0b1011, (3, 2): 0b1110, (3, 3): 0b1111,
    }
    for coords, want in expected.items():
        assert encode(coords, s) == want
        assert ref_interleave(coords, (2, 2)) == want


def test_encode_matches_independent_interleaver_exhaustive():
    for dims in [(2, 2), (3, 2), (2, 3), (4, 1), (1, 4), (3, 3, 2)]:
        s = SchemaSpec(dims=tuple(DimSpec(f"d{i}", b, signed=False) for i, b in enumerate(dims)))
        ranges = [range(1 << b) for b in dims]
        for coords in itertools.product(*ranges):
            code = encode(coords, s)
            assert code == ref_interleave(coords, dims)
            # production decode agrees with the independent deinterleaver
            assert ref_deinterleave(code, dims) == coords
            assert decode(code, s) == coords


def test_signed_roundtrip_exhaustive_3x3():
    s = SchemaSpec(dims=(DimSpec("x", 3, signed=True), DimSpec("y", 3, signed=True)))
    for v, w in itertools.product(range(-4, 4), repeat=2):
        assert decode(encode((v, w), s), s) == (v, w)


def test_128bit_high_bits_are_not_truncated():
    s = SchemaSpec(dims=tuple(DimSpec(f"d{i}", 16, signed=True) for i in range(8)))
    assert s.total_bits == 128
    extreme = tuple([-32768] + [32767] * 7)
    code = encode(extreme, s)
    hi, lo = split128(code)
    assert code >= 1 << 127, "sign-corner MSB must survive"
    assert combine128(hi, lo) == code
    assert decode(code, s) == extreme
    # and the opposite corner reaches a low code without losing a high dim bit
    other = tuple([0] * 8)
    assert decode(encode(other, s), s) == other


def test_decode_rejects_code_wider_than_schema():
    s = SchemaSpec(dims=(DimSpec("x", 2, signed=False), DimSpec("y", 2, signed=False)))
    with pytest.raises(ValueError):
        decode(1 << 4, s)


# --------------------------------------------------------------------------- #
# decomposition geometry on the 2x2-bit universe (16 points)
# --------------------------------------------------------------------------- #
S2 = SchemaSpec(dims=(DimSpec("x", 2, signed=False), DimSpec("y", 2, signed=False)))
POINT_CODE = {(x, y): encode((x, y), S2) for x in range(4) for y in range(4)}


def covered_points(intervals):
    out = set()
    for x in range(4):
        for y in range(4):
            c = POINT_CODE[(x, y)]
            if any(iv.lo <= c <= iv.hi for iv in intervals):
                out.add((x, y))
    return out


def test_full_universe_single_exact_interval():
    r = decompose_box(S2, (0, 0), (3, 3), 64)
    assert r.intervals == (Interval(0, 15, True),)
    assert r.budget_exhausted is False
    assert r.cells_split == 0


def test_point_box_resolves_to_single_exact_code():
    r = decompose_box(S2, (1, 1), (1, 1), 64)
    assert all(iv.exact for iv in r.intervals)
    singletons = [(iv.lo, iv.hi) for iv in r.intervals if iv.lo == iv.hi]
    assert singletons == [(POINT_CODE[(1, 1)], POINT_CODE[(1, 1)])]
    assert covered_points(r.intervals) == {(1, 1)}


def test_thin_vertical_box_two_exact_intervals():
    # x fixed to 1, y spans everything: codes 2,3,6,7
    r = decompose_box(S2, (1, 0), (1, 3), 64)
    assert r.intervals == (Interval(2, 3, True), Interval(6, 7, True))
    assert covered_points(r.intervals) == {(1, y) for y in range(4)}
    assert r.conservative_intervals == 0


def test_thin_horizontal_box():
    # y fixed to 2: codes of (x,2) = 4,6,12,14
    r = decompose_box(S2, (0, 2), (3, 2), 64)
    want = {(x, 2) for x in range(4)}
    assert covered_points(r.intervals) == want
    assert all(iv.exact for iv in r.intervals)


def test_disjoint_box_request_rejected_at_validation():
    with pytest.raises(ValueError):
        decompose_box(S2, (0, 0), (4, 0), 64)


def test_budget_exhaustion_widens_but_never_drops():
    lo, hi = (1, 0), (2, 3)
    truth = {(x, y) for x in range(1, 3) for y in range(4)}
    for budget in (1, 2, 3, 4):
        r = decompose_box(S2, lo, hi, budget)
        assert truth.issubset(covered_points(r.intervals)), f"missing rows at budget {budget}"
    r1 = decompose_box(S2, lo, hi, 1)
    assert r1.budget_exhausted is True
    assert r1.conservative_intervals >= 1
    # the tightest budget covers the whole universe here (root emitted whole)
    assert covered_points(r1.intervals) == set(POINT_CODE)


def test_exact_intervals_contain_no_outside_points():
    """An interval labeled exact must not cover any point outside the box."""
    cases = [((0, 0), (2, 1)), ((1, 1), (3, 2)), ((0, 2), (2, 3))]
    for lo, hi in cases:
        r = decompose_box(S2, lo, hi, 64)
        truth = {(x, y) for x in range(lo[0], hi[0] + 1) for y in range(lo[1], hi[1] + 1)}
        inside_exact = set()
        for x in range(4):
            for y in range(4):
                c = POINT_CODE[(x, y)]
                if any(iv.exact and iv.lo <= c <= iv.hi for iv in r.intervals):
                    inside_exact.add((x, y))
        assert inside_exact.issubset(truth)
        assert truth.issubset(covered_points(r.intervals))


def test_more_budget_never_increases_conservative_footprint():
    lo, hi = (0, 1), (2, 2)
    prev_cover = set(POINT_CODE)
    for budget in (1, 2, 4, 8, 64):
        cover = covered_points(decompose_box(S2, lo, hi, budget).intervals)
        truth = {(x, y) for x in range(0, 3) for y in range(1, 3)}
        assert truth.issubset(cover)
        assert cover.issubset(prev_cover)
        prev_cover = cover


def test_unequal_bit_widths_roundtrip_and_cover():
    s = SchemaSpec(dims=(DimSpec("a", 2, signed=False), DimSpec("b", 4, signed=False)))
    truth = set()
    for a in range(4):
        for b in range(16):
            assert decode(encode((a, b), s), s) == (a, b)
            if a == 1 and 2 <= b <= 5:
                truth.add((a, b))
    r = decompose_box(s, (1, 2), (1, 5), 64)
    got = set()
    for a in range(4):
        for b in range(16):
            c = encode((a, b), s)
            if any(iv.lo <= c <= iv.hi for iv in r.intervals):
                got.add((a, b))
    assert truth == got
    assert all(iv.exact for iv in r.intervals)


def test_signed_box_uses_unsigned_edges_via_kernel_mapping():
    # mapping unit test: signed range [-2,1] straddling zero
    d = DimSpec("x", 4, signed=True)
    from zindex.kernel import raw_box_to_unsigned
    s = SchemaSpec(dims=(d, DimSpec("y", 4, signed=True)))
    ulo, uhi = raw_box_to_unsigned(s, [-2, -3], [1, 2])
    # zz values: -2->3,1->2 => [0,3]; -3->5,2->4 => [0,5]
    assert ulo == (0, 0)
    assert uhi == (3, 5)
