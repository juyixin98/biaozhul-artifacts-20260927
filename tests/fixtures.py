"""Synthetic local fixtures -- no production data or external actors."""
from __future__ import annotations

from .oracle import RefBatch


def repeated_dict_items():
    """Batch b1 binds 'a' to two local codes (0 and 2); b2 independently
    binds its local code 1 to 'b' and code 0 to 'z'. Exercises:
    same value / different local code MUST merge; same local code /
    different value across batches MUST NOT."""
    return [
        RefBatch("b1", ("a", "b", "a"), (0, 1, 2, 0, -1),
                 (True, True, True, True, False)),
        RefBatch("b2", ("z", "b"), (0, 1, 0), (True, True, True)),
    ]


def empty_and_nulls():
    """One empty dictionary batch with zero rows; one batch where every
    row is NULL and the dictionary is empty."""
    return [
        RefBatch("empty", (), (), ()),
        RefBatch("all_null", (), (-1, -1, -1), (False, False, False)),
    ]


def all_null_with_declared_dict():
    """Every row NULL but a (non-empty) dictionary was declared: values
    remain in the global dictionary yet no valid row references them."""
    return [
        RefBatch("ghosts", ("x", "y"), (-1, -1), (False, False)),
    ]


def width_boundary(width=8):
    """Exactly 256 distinct ints (fills 8-bit) plus 257th scenario handled
    by the overflow tests."""
    vals = tuple(range(256))
    return [RefBatch("full8", vals, tuple(range(256)),
                     tuple([True] * 256))]


def overflow_257():
    vals = tuple(range(257))
    return [RefBatch("over8", vals, tuple(range(257)),
                     tuple([True] * 257))]


def permutation_triplet():
    """Three batches/orders used to show remap invariance to input order."""
    return [
        RefBatch("p1", ("m", "n"), (0, 1), (True, True)),
        RefBatch("p2", ("n", "o"), (1, 0), (True, True)),
        RefBatch("p3", ("m", "o"), (1, 0), (True, True)),
    ]


def typed_mix():
    """int64 and utf8 coexist; ints sort before strings, and numeric 1 is
    never merged with string '1'."""
    return [
        RefBatch("nums", (2, 1, 10), (0, 1, 2), (True, True, True)),
        RefBatch("strs", ("1", "a"), (0, 1), (True, True)),
    ]
