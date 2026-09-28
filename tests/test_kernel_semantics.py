"""Kernel semantics against the independent oracle and hardcoded values."""
from __future__ import annotations

import pytest

from dictsvc.core import (
    BatchInput, CardinalityOverflow, DictionaryContainsNull,
    DuplicateDictionaryValue, IndexOutOfRange, encode_run,
    verify_roundtrip,
)

from . import fixtures
from .conftest import to_batch_input
from .oracle import decode_ref_encoding, expected_decoded_rows, expected_encoding


def _inputs(refs):
    return [to_batch_input(b) for b in refs]


def test_repeated_dict_items_merge_and_local_codes_do_not_collide():
    refs = fixtures.repeated_dict_items()
    enc = encode_run(_inputs(refs))

    # Hardcoded global dictionary: sorted utf8 a,b,z.
    assert enc.global_types == ("utf8", "utf8", "utf8")
    assert enc.global_values == ("a", "b", "z")
    # b1's repeated item ('a' at local 0 AND local 2) -> both map to 0.
    b1 = enc.batches[0]
    assert b1.local_to_global == (0, 1, 0)
    assert list(b1.global_indices) == [0, 1, 0, 0, -1]
    # NULL is carried only by the bitmap: last row valid=False with -1.
    assert list(b1.valid) == [True, True, True, True, False]
    # b2's local code 0 ('z') must NOT merge with b1's local 0 ('a').
    b2 = enc.batches[1]
    assert b2.local_to_global == (2, 1)
    assert list(b2.global_indices) == [2, 1, 2]

    assert b1.stats.duplicate_declared == 1
    assert b1.stats.distinct_values == 2

    # Cross-check against the independent oracle.
    ref = expected_encoding(refs)
    assert ref.global_values == ("a", "b", "z")
    assert ref.local_to_global["b1"] == (0, 1, 0)
    assert ref.local_to_global["b2"] == (2, 1)

    # Every decoded row equals the original batch row.
    report = verify_roundtrip(enc, {b.batch_id: b for b in _inputs(refs)})
    assert report.all_match is True
    assert report.checked_rows == 8
    assert decode_ref_encoding(ref) == expected_decoded_rows(refs)


def test_empty_dictionary_and_all_null_batches():
    refs = fixtures.empty_and_nulls()
    enc = encode_run(_inputs(refs))

    assert enc.cardinality == 0
    assert enc.global_values == ()
    # Empty dictionary keeps the smallest ladder width, 8 bits.
    assert enc.global_index_width == 8

    empty, all_null = enc.batches
    assert empty.local_to_global == ()
    assert list(empty.global_indices) == []
    assert all_null.stats.row_count == 3
    assert all_null.stats.null_rows == 3
    # NULL never occupied a value code.
    assert list(all_null.global_indices) == [-1, -1, -1]
    assert list(all_null.valid) == [False, False, False]

    ref = expected_encoding(refs)
    assert ref.cardinality == 0 and ref.width == 8
    report = verify_roundtrip(enc, {b.batch_id: b for b in _inputs(refs)})
    assert report.all_match is True


def test_all_null_rows_with_declared_values_are_unused_not_dropped():
    refs = fixtures.all_null_with_declared_dict()
    enc = encode_run(_inputs(refs))
    (rb,) = enc.batches
    # Declared values stay in the global dictionary ...
    assert enc.global_values == ("x", "y")
    assert rb.local_to_global == (0, 1)
    # ... but are reported as unused; rows are still all NULL.
    assert rb.stats.used_entries == 0
    assert rb.stats.unused_declared == 2
    assert list(rb.global_indices) == [-1, -1]
    assert all(v is False for v in rb.valid)
    report = verify_roundtrip(enc, {b.batch_id: b for b in _inputs(refs)})
    assert report.all_match is True


def test_null_value_inside_dictionary_is_rejected_not_encoded():
    b = BatchInput("bad", ("a", None, "c"), (0, 1), (True, True))
    with pytest.raises(DictionaryContainsNull) as exc:
        encode_run([b])
    assert exc.value.batch_id == "bad"
    assert exc.value.details["local_code"] == 1


def test_valid_row_index_out_of_range_is_rejected():
    b = BatchInput("oob", ("a",), (0, 5), (True, True))
    with pytest.raises(IndexOutOfRange) as exc:
        encode_run([b])
    assert exc.value.details == {"row": 1, "index": 5, "dictionary_size": 1}


def test_out_of_range_index_at_null_row_is_not_dereferenced():
    # Bitmap independence: an invalid row never dereferences its slot.
    b = BatchInput("nullslot", ("a",), (99,), (False,))
    enc = encode_run([b])
    (rb,) = enc.batches
    assert list(rb.valid) == [False]
    assert list(rb.global_indices) == [-1]


def test_strict_duplicate_policy_rejects_repeated_dict_items():
    b = BatchInput("strict", ("a", "b", "a"), (0,), (True,))
    with pytest.raises(DuplicateDictionaryValue) as exc:
        encode_run([b], on_duplicate_values="error")
    assert exc.value.details["local_code_a"] == 0
    assert exc.value.details["local_code_b"] == 2


def test_cardinality_boundaries_256_fits_257_rejects_and_expands():
    fit = encode_run(_inputs(fixtures.width_boundary()), target_width=8)
    assert fit.cardinality == 256
    assert fit.global_index_width == 8

    with pytest.raises(CardinalityOverflow) as exc:
        encode_run(_inputs(fixtures.overflow_257()), target_width=8)
    assert exc.value.details["required_width"] == 16
    assert exc.value.details["target_capacity"] == 256

    expanded = encode_run(_inputs(fixtures.overflow_257()),
                          target_width=8, width_policy="expand")
    assert expanded.cardinality == 257
    assert expanded.global_index_width == 16


def test_fixed_sort_policy_type_then_value():
    refs = fixtures.typed_mix()
    enc = encode_run(_inputs(refs))
    # int64 rank precedes utf8; ints numeric; strings bytewise; '1' != 1.
    assert list(zip(enc.global_types, enc.global_values)) == [
        ("int64", 1), ("int64", 2), ("int64", 10),
        ("utf8", "1"), ("utf8", "a"),
    ]
    ref = expected_encoding(refs)
    assert list(zip(ref.global_types, ref.global_values)) == \
        list(zip(enc.global_types, enc.global_values))
    report = verify_roundtrip(enc, {b.batch_id: b for b in _inputs(refs)})
    assert report.all_match is True
    assert report.checked_rows == 5


def test_order_independence_scope():
    """Claim under test: remap output is invariant under (a) batch order,
    (b) intra-batch row order. Scope: only as long as the VALUE SET is
    unchanged. A changed value set legitimately changes codes."""
    refs = fixtures.permutation_triplet()

    base = encode_run(_inputs(refs))
    # (a) permute batches
    swapped = encode_run(_inputs([refs[1], refs[0], refs[2]]))
    assert swapped.global_values == base.global_values
    for bid in ("p1", "p2", "p3"):
        a = next(x for x in base.batches if x.batch_id == bid)
        b = next(x for x in swapped.batches if x.batch_id == bid)
        assert a.local_to_global == b.local_to_global

    # (b) permute rows within a batch: codes follow the rows, same mapping
    p1_reversed = BatchInput(
        "p1", ("m", "n"), (1, 0), (True, True))
    rev = encode_run([p1_reversed, refs[1], refs[2]])
    assert next(x for x in rev.batches if x.batch_id == "p1") \
        .local_to_global == (0, 1)
    r0 = next(x for x in rev.batches if x.batch_id == "p1")
    assert list(r0.global_indices) == [1, 0]

    # Scope boundary: removing a value CHANGES the set, so codes may shift;
    # we assert that documented limitation rather than promising stability.
    reduced = encode_run(_inputs([refs[0]]))  # only m,n -> codes 0,1
    assert reduced.global_values == ("m", "n")
