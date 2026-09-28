"""Morton codec tests.

Expected codes are hand-derived, not produced by the tested implementation;
cross-module agreement is additionally checked against ``reference.naive``,
which builds codes by a completely different (string-position) algorithm.
"""

from __future__ import annotations

import itertools

import pytest

from reference.naive import deinterleave, spread_interleave
from zcluster.kernel.coder import DimSpec, MortonCoder, OutOfDomainError


def test_known_2d_vector():
    # 2-bit unsigned; convention: bit j of dim d sits at j*2+d (dim0 = even
    # slot). point (3,1): u=11 (positions 0,2), v=01 (position 1) => 0111b = 7
    coder = MortonCoder([DimSpec("u", 2), DimSpec("v", 2)])
    assert coder.encode([3, 1]) == 0b0111
    assert coder.encode([0, 0]) == 0
    assert coder.encode([3, 3]) == 0b1111
    # Z-order on a 2x2 grid: (0,0)=0, (1,0)=1, (0,1)=2, (1,1)=3
    assert coder.encode([1, 0]) == 1
    assert coder.encode([0, 1]) == 2
    assert coder.decode(0b0111) == [3, 1]


def test_signed_mapping_is_order_preserving_and_known():
    d = DimSpec("x", 4, signed=True)
    assert d.to_unsigned(-8) == 0
    assert d.to_unsigned(-1) == 7
    assert d.to_unsigned(0) == 8
    assert d.to_unsigned(7) == 15
    assert d.from_unsigned(0) == -8
    assert d.from_unsigned(15) == 7
    # signed zero maps to the middle, not zero
    coder = MortonCoder([DimSpec("x", 4, signed=True), DimSpec("y", 2)])
    assert coder.encode([0, 0]) == spread_interleave([8, 0], [4, 2])


def test_high_bits_never_truncated_max_corner():
    for bits, ndim in ((2, 2), (5, 3), (8, 8), (16, 4)):
        dims = [DimSpec(f"d{i}", bits) for i in range(ndim)]
        coder = MortonCoder(dims)
        top = coder.encode([(1 << bits) - 1] * ndim)
        # all total_bits positions set
        assert top == (1 << coder.total_bits) - 1
        assert top.bit_length() == coder.total_bits
        zero = coder.encode([0] * ndim)
        assert zero == 0


def test_unequal_widths_pad_high_dimension_zeros():
    coder = MortonCoder([DimSpec("a", 3), DimSpec("b", 1)])
    # positions: j=2 -> only dim0 bit at 2*2+0 = 4
    assert coder.encode([4, 1]).bit_length() <= 5
    assert coder.encode([4, 1]) == spread_interleave([4, 1], [3, 1])
    assert coder.total_bits == 3 * 2


def test_roundtrip_full_domain_2d_against_reference():
    widths = [3, 2]
    dims = [DimSpec("a", widths[0]), DimSpec("b", widths[1])]
    coder = MortonCoder(dims)
    for u, v in itertools.product(range(8), range(4)):
        code = coder.encode([u, v])
        assert code == spread_interleave([u, v], widths)
        assert coder.decode(code) == [u, v]
        assert deinterleave(code, widths) == [u, v]


def test_signed_roundtrip_boundaries_and_negatives():
    dims = [DimSpec("x", 4, signed=True), DimSpec("y", 3, signed=True)]
    coder = MortonCoder(dims)
    for x in (-8, -7, -1, 0, 1, 6, 7):
        for y in (-4, -1, 0, 3):
            assert coder.decode(coder.encode([x, y])) == [x, y]


def test_out_of_domain_reports_dimension_and_limits():
    coder = MortonCoder([DimSpec("x", 3, signed=True), DimSpec("y", 2)])
    with pytest.raises(OutOfDomainError, match="x.*outside"):
        coder.encode([-5, 0])
    with pytest.raises(OutOfDomainError, match="x.*outside"):
        coder.encode([4, 0])
    with pytest.raises(OutOfDomainError, match="y"):
        coder.encode([0, 4])


def test_bad_specs_are_rejected():
    with pytest.raises(ValueError):
        DimSpec("x", 0)
    with pytest.raises(ValueError):
        DimSpec("x", 65)
    with pytest.raises(ValueError):
        MortonCoder([DimSpec("x", 2), DimSpec("x", 2)])
    with pytest.raises(TypeError):
        DimSpec("x", 2).to_unsigned(True)
