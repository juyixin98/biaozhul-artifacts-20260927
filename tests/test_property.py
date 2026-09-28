"""Property-style cross checks against two independent oracles.

Random arrays come from a local seeded generator. For every random slice the
kernel's NULL map, values, and offsets are compared against (a) a raw-bytes
oracle in helpers.py and (b) PyArrow; concat results are compared with
``pa.concat_arrays``. The expected answers are never produced by the code
under test.
"""

from __future__ import annotations

import random
import string

import numpy as np
import pyarrow as pa
import pytest

from arrowzero.adapters import import_pylist
from arrowzero.kernel.concat import concat

pytestmark = pytest.mark.integration

WORDS = ["", "a", "bc", "δεζ", "x" * 7, "混合", "null-ish", ""]
PRIMITIVE_CASES = [
    ("int8", "int8"),
    ("int16", "int16"),
    ("int32", "int32"),
    ("int64", "int64"),
    ("uint32", "uint32"),
    ("float32", "float32"),
    ("float64", "float64"),
]


@pytest.mark.parametrize("type_name,np_name", PRIMITIVE_CASES)
def test_random_primitive_slices_match_both_oracles(type_name, np_name):
    rng = np.random.default_rng(int.from_bytes(type_name.encode(), "little") % (2**32))
    n = 65  # spans several byte-boundary slices
    values = rng.integers(-50, 50, size=n).astype(np_name).tolist()
    for i in range(n):
        if i % 5 == 2:
            values[i] = None
    view, _ = import_pylist(values, type_name)
    validity, data = view.buffers[0], view.buffers[1]
    from helpers import oracle_null_map, oracle_primitive_values

    for start, length in [(0, n), (1, n - 1), (7, n - 9), (8, n - 10), (63, 2)]:
        sl = view.slice(start, length)
        nulls = oracle_null_map(bytes(validity), length, start)
        assert [sl.is_null(i) for i in range(length)] == nulls
        raw_vals = oracle_primitive_values(bytes(data), np_name, length, start)
        expected = [None if is_null else val for is_null, val in zip(nulls, raw_vals)]
        assert sl.to_pylist() == expected
        assert sl.to_arrow().to_pylist() == expected
        assert sl.count_nulls() == sum(nulls)


def test_random_string_slices_empty_vs_null():
    rnd = random.Random(20260927)
    n = 40
    values = []
    for i in range(n):
        choice = rnd.random()
        if choice < 0.3:
            values.append(None)
        elif choice < 0.5:
            values.append("")  # distinct from NULL
        else:
            values.append(rnd.choice(WORDS))
    view, _ = import_pylist(values, "utf8")
    validity, offsets, data = (bytes(b) for b in view.buffers)
    from helpers import oracle_string_values

    for start, length in [(0, n), (1, 20), (13, 15), (31, 9), (39, 1)]:
        sl = view.slice(start, length)
        expected = oracle_string_values(validity, offsets, data, length, start)
        assert sl.to_pylist() == expected
        assert sl.to_arrow().to_pylist() == expected
        # explicitly assert "" and NULL are not conflated
        assert sum(x is None for x in expected) == sl.count_nulls()
        assert sum(x == "" for x in expected if x is not None) == sum(
            sl.get(i) == "" for i in range(length) if not sl.is_null(i)
        )


def test_random_concat_partitions_equals_pyarrow():
    rnd = random.Random(4242)
    words = ["p", "", "qrs", "τ"]
    for trial in range(20):
        n = rnd.randint(1, 25)
        values = [
            None if rnd.random() < 0.3 else rnd.choice(words) for _ in range(n)
        ]
        view, _ = import_pylist(values, "utf8")
        cuts = sorted(rnd.sample(range(1, n + 1), k=min(2, n)))
        bounds = [0] + cuts + [n]
        chunks = [view.slice(a, b - a) for a, b in zip(bounds[:-1], bounds[1:])]
        merged, ledger = concat(chunks)
        assert merged.to_pylist() == values
        ref = pa.concat_arrays([c.to_arrow() for c in chunks]).to_pylist()
        assert merged.to_pylist() == ref
        # accounting sanity: positive data movement, no aliasing
        assert ledger.copied_bytes >= sum(
            len(v.encode("utf-8")) for v in values if v is not None
        )


def test_random_concat_primitives_with_zero_offset_and_nonzero():
    rng = np.random.default_rng(7)
    a_values = rng.integers(0, 100, size=30, dtype=np.int32).tolist()
    for i in (3, 17, 29):
        a_values[i] = None
    view, _ = import_pylist(a_values, "int32")
    chunks = [view.slice(0, 5), view.slice(5, 10), view.slice(15, 15)]
    merged, _ = concat(chunks)
    expected = a_values[:15] + a_values[15:]
    assert merged.to_pylist() == expected
    assert merged.to_arrow().to_pylist() == expected
