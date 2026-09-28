"""RLE / bit-packed hybrid level codec tests, incl. cross-implementation."""
from __future__ import annotations

import numpy as np
import pytest

from app.core.levels_codec import (
    bit_width,
    decode_hybrid,
    decode_levels_v1,
    encode_hybrid,
    encode_levels_v1,
)


@pytest.mark.parametrize("seed", range(200))
def test_self_roundtrip_random(seed):
    import random
    random.seed(seed)
    width = random.randint(1, 16)
    n = random.randint(0, 500)
    vals = [random.randint(0, (1 << width) - 1) for _ in range(n)]
    assert decode_hybrid(encode_hybrid(vals, width), width, n) == vals


def test_all_widths_with_long_runs():
    for width in range(1, 17):
        vals = list(range(0, min(20, 1 << width))) * 5 + [0] * 300
        assert decode_hybrid(encode_hybrid(vals, width), width, len(vals)) \
            == vals


def test_v1_length_prefix_roundtrip():
    vals = [0, 1, 1, 0, 0, 3, 2, 3]
    buf = encode_levels_v1(vals, 3)
    levels, offset = decode_levels_v1(buf, 3, len(vals))
    assert levels == vals
    assert offset == len(buf)


def test_empty_stream():
    assert decode_hybrid(encode_hybrid([], 3), 3, 0) == []


def test_bit_width_matches_max_level():
    assert bit_width(0) == 0
    assert bit_width(1) == 1
    assert bit_width(3) == 2
    assert bit_width(5) == 3
    assert bit_width(7) == 3
    assert bit_width(8) == 4


def test_our_encoding_read_by_fastparquet():
    """Our encoded bytes must be accepted by fastparquet's own decoder."""
    fp = pytest.importorskip("fastparquet.cencoding")
    import random
    random.seed(4242)
    for _ in range(50):
        width = random.randint(1, 8)
        n = random.randint(1, 300)
        vals = [random.randint(0, (1 << width) - 1) for _ in range(n)]
        payload = encode_hybrid(vals, width)
        out = np.empty(n, dtype="uint8")
        fp.read_rle_bit_packed_hybrid(
            fp.NumpyIO(payload), width, len(payload),
            o=fp.NumpyIO(out), itemsize=1,
        )
        assert out.tolist() == vals
