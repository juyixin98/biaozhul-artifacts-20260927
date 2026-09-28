"""Kernel tests: concrete expected results from the independent oracle plus
explicit failure-category assertions.

Required questions covered:
  Q1 duplicate dictionary entries
  Q2 empty dictionary
  Q3 all-NULL batches
  Q4 large-cardinality width thresholds (255/256/65535/65536)
  Q5 decoded per-row equality with original batch
  Q6 scope of the "order independence" claim (see test_order_independence.py)
"""
from __future__ import annotations

import pytest

from app.core import errors
from app.core.kernel import BatchInput, unify
from app.core.verify import decode_rows, verify_roundtrip
from tests.fixtures import make_batch
from tests.oracle import oracle_unify


def to_kernel(raw: list[dict]) -> list[BatchInput]:
    out = []
    for rb in raw:
        validity = (rb["validity"] if rb.get("validity") is not None
                    else [True] * len(rb["indices"]))
        out.append(BatchInput(rb["batch_id"], list(rb["dictionary"]),
                              list(rb["indices"]), list(validity)))
    return out


# --------------------------------------------------------------- Q: merging

def test_same_value_different_local_codes_merge(overlapping_batches, step_logger):
    """'a' appears under b0:0 and b1:2; 'b' under b0:1 and b1:0."""
    result = unify(to_kernel(overlapping_batches), value_type="string")
    step_logger.step("unify", cardinality=result.cardinality,
                     width=result.index_width_bits)

    # concrete, hand-checked global dictionary and codes
    assert result.global_dictionary == ("a", "b", "c")
    by_id = {r.batch_id: r for r in result.batch_remaps}
    assert by_id["b0"].local_to_global == (0, 1)
    assert by_id["b1"].local_to_global == (1, 2, 0)

    oracle = oracle_unify(overlapping_batches, value_type="string")
    assert tuple(result.global_dictionary) == oracle.global_dictionary
    for bid, (l2g, gidx, validity) in oracle.remaps.items():
        assert list(by_id[bid].local_to_global) == l2g
        assert list(by_id[bid].global_indices) == gidx


def test_same_local_code_different_values_must_not_merge(step_logger):
    """b0 code 0='x', b1 code 0='z': merging by code would collapse them."""
    raw = [
        make_batch("b0", ["x", "y"], [0, 1, 0]),
        make_batch("b1", ["z", "y"], [0, 1, 1]),
    ]
    result = unify(to_kernel(raw), value_type="string")
    step_logger.step("unify", gdict=list(result.global_dictionary))

    assert result.global_dictionary == ("x", "y", "z")
    by_id = {r.batch_id: r for r in result.batch_remaps}
    assert by_id["b0"].local_to_global == (0, 1)
    assert by_id["b1"].local_to_global == (2, 1)
    # decoded value of b1's first row must be 'z', not 'x'
    decoded = {d.batch_id: d.rows for d in decode_rows(result)}
    assert decoded["b1"][0] == "z"
    assert decoded["b0"][0] == "x"


# --------------------------------------------------------------- Q: round-trip

def test_decode_matches_every_original_row(overlapping_batches, step_logger):
    batches = to_kernel(overlapping_batches)
    result = unify(batches, value_type="string")
    decoded = verify_roundtrip(result, batches)
    for d in decoded:
        src = next(b for b in batches if b.batch_id == d.batch_id)
        expected = tuple(
            None if not v else src.dictionary[src.indices[i]]
            for i, v in enumerate(src.validity)
        )
        step_logger.step("rows-checked", batch=d.batch_id,
                         expected=list(expected), actual=list(d.rows))
        assert d.rows == expected
    # NULL counts are explicit
    nulls = {d.batch_id: d.null_count for d in result.batch_remaps}
    assert nulls == {"b0": 1, "b1": 2}


# --------------------------------------------------------------- Q: NULL bitmap

def test_null_does_not_consume_value_semantics(step_logger):
    """A NULL row's index slot is padding 0 but is never decoded as dict[0]."""
    raw = [make_batch("b", ["only"], [0, 0, 0], [True, False, True])]
    result = unify(to_kernel(raw), value_type="string")
    r = result.batch_remaps[0]
    assert r.validity == (True, False, True)
    assert r.global_indices == (0, 0, 0)  # padding is 0
    decoded = decode_rows(result)[0].rows
    step_logger.step("null-decode", rows=list(decoded))
    assert decoded == ("only", None, "only")


def test_validity_length_mismatch_is_classified():
    raw = [make_batch("b", ["a"], [0], [True, False])]
    with pytest.raises(errors.InvalidValidityError) as ei:
        unify(to_kernel(raw), value_type="string")
    assert ei.value.details["validity_len"] == 2
    assert ei.value.details["indices_len"] == 1


def test_null_entry_inside_dictionary_rejected():
    raw = [make_batch("b", ["a", None], [0, 1])]
    # The JSON adapter rejects; emulate kernel-level normalized path directly:
    # kernel operates on Python values, None is simply not a string value.
    with pytest.raises(errors.ValueTypeMismatchError):
        unify(to_kernel(raw), value_type="string")


# --------------------------------------------------------------- Q2/Q3 empties

def test_empty_dictionary_with_all_null_rows(empty_dictionary_batch, step_logger):
    result = unify(to_kernel([empty_dictionary_batch]), value_type="string")
    step_logger.step("empty-dict", cardinality=result.cardinality,
                     width=result.index_width_bits)
    assert result.cardinality == 0
    assert result.global_dictionary == ()
    # canonical smallest width even when there are no value codes
    assert result.index_width_bits == 8
    r = result.batch_remaps[0]
    assert r.null_count == 4 and r.row_count == 4
    decoded = decode_rows(result)[0].rows
    assert decoded == (None, None, None, None)


def test_all_null_large_batch_with_nonempty_dict():
    """Dictionary can exist while every row is NULL: cardinality still counts
    the declared distinct values (they are encodable in later batches)."""
    raw = [make_batch("b", ["a", "b"], [0, 0, 0],
                      [False, False, False])]
    result = unify(to_kernel(raw), value_type="string")
    assert result.cardinality == 2
    assert all(v is False for v in result.batch_remaps[0].validity)
    assert decode_rows(result)[0].rows == (None, None, None)


def test_empty_request_rejected():
    with pytest.raises(errors.EmptyRequestError) as ei:
        unify([], value_type="string")
    assert ei.value.code == "EMPTY_REQUEST"


def test_valid_row_against_empty_dictionary_is_out_of_range():
    # NULL rows are fine with an empty dict; a *valid* row cannot resolve.
    raw = [make_batch("b", [], [0], [True])]
    with pytest.raises(errors.IndexOutOfRangeError):
        unify(to_kernel(raw), value_type="string")


def test_nonzero_padding_on_null_row_rejected_by_verifier():
    """The padding convention (NULL -> code 0) is enforced by the oracle so
    NULL can never be silently decoded as a value."""
    raw = [make_batch("b", ["a", "b"], [0, 1], [True, False])]
    result = unify(to_kernel(raw), value_type="string")
    r = result.batch_remaps[0]
    tampered = type(result)(
        result.global_dictionary, result.global_value_type,
        result.index_width_bits, result.cardinality, result.sort_policy,
        (type(r)(r.batch_id, r.local_to_global, (0, 1), r.validity,
                 r.row_count, r.null_count),),
        result.stats,
    )
    with pytest.raises(errors.VerificationMismatchError) as ei:
        verify_roundtrip(tampered, to_kernel(raw))
    assert "padding" in ei.value.message


# --------------------------------------------------------------- Q1 duplicates

def test_duplicate_local_dictionary_entry_is_rejected_by_kernel(duplicate_dict_batch):
    with pytest.raises(errors.DuplicateValueInDictionaryError) as ei:
        unify(to_kernel([duplicate_dict_batch]), value_type="string")
    d = ei.value.details
    assert d["first_code"] == 0 and d["duplicate_code"] == 1
    assert d["value_repr"] == "'x'"


def test_duplicate_batch_id_rejected():
    raw = [make_batch("x", ["a"], [0]), make_batch("x", ["b"], [0])]
    with pytest.raises(errors.DuplicateBatchIdError):
        unify(to_kernel(raw), value_type="string")


# --------------------------------------------------------------- Q4 widths

@pytest.mark.parametrize("n,expected_width", [
    (1, 8), (255, 8), (256, 16), (65535, 16), (65536, 32),
])
def test_auto_width_boundaries(n, expected_width, step_logger):
    raw = [make_batch("b", [f"v{i}" for i in range(n)], list(range(n)))]
    result = unify(to_kernel(raw), value_type="string")
    step_logger.step("width-boundary", n=n, width=result.index_width_bits)
    assert result.cardinality == n
    assert result.index_width_bits == expected_width


def test_strict_width_overflow_has_dedicated_category():
    raw = [make_batch("b", [f"v{i}" for i in range(300)], list(range(300)))]
    with pytest.raises(errors.IndexWidthOverflowError) as ei:
        unify(to_kernel(raw), value_type="string",
              index_policy="strict", target_width=8)
    assert ei.value.details["capacity"] == 255
    assert ei.value.details["cardinality"] == 300


def test_strict_width_ok_when_fits():
    raw = [make_batch("b", ["a", "b"], [0, 1])]
    result = unify(to_kernel(raw), value_type="string",
                   index_policy="strict", target_width=32)
    assert result.index_width_bits == 32


def test_hard_cardinality_limit_rejected():
    # Use a small configured ceiling to exercise the same path as uint32 max.
    raw = [make_batch("b", [f"v{i}" for i in range(10)], list(range(10)))]
    with pytest.raises(errors.CardinalityLimitError) as ei:
        unify(to_kernel(raw), value_type="string", max_cardinality=9)
    assert ei.value.details["max_cardinality"] == 9


def test_huge_cardinality_hard_cap_skipped():
    """Building 2^32+1 distinct strings is impractical in CI.

    The guard logic is covered via the configurable ceiling
    (``test_hard_cardinality_limit_rejected``); this test documents the real
    boundary and stays SKIPPED so the test report honestly records it as
    not executed rather than pretending to run it.
    """
    pytest.skip("requires 2^32+1 distinct values; path covered via "
                "max_cardinality=9 (see test_hard_cardinality_limit_rejected)")


# --------------------------------------------------------------- index checks

def test_negative_index_is_invalid_index_not_range_error():
    raw = [make_batch("b", ["a"], [-1])]
    with pytest.raises(errors.InvalidIndexError) as ei:
        unify(to_kernel(raw), value_type="string")
    assert ei.value.details["index"] == -1


def test_index_out_of_range_is_distinct_category():
    raw = [make_batch("b", ["a"], [1])]
    with pytest.raises(errors.IndexOutOfRangeError) as ei:
        unify(to_kernel(raw), value_type="string")
    assert ei.value.details["dictionary_size"] == 1
    assert ei.value.details["index"] == 1


def test_bool_is_not_an_index():
    raw = [make_batch("b", ["a"], [True])]
    with pytest.raises(errors.InvalidIndexError):
        unify(to_kernel(raw), value_type="string")


# --------------------------------------------------------------- value types

def test_bool_value_is_not_merged_with_int():
    raw = [make_batch("b", [True], [0])]
    result = unify(to_kernel(raw), value_type="bool")
    assert result.global_dictionary == (True,)
    with pytest.raises(errors.ValueTypeMismatchError):
        unify(to_kernel([make_batch("x", [1], [0])]), value_type="bool")


def test_double_normalizes_int_and_rejects_nan():
    raw = [make_batch("b", [1, 1.0, 2.0], [0, 1, 2])]
    # 1 and 1.0 are the same double -> duplicate within local dictionary
    with pytest.raises(errors.DuplicateValueInDictionaryError):
        unify(to_kernel(raw), value_type="double")
    with pytest.raises(errors.ValueTypeMismatchError):
        unify(to_kernel([make_batch("x", [float("nan")], [0])]),
              value_type="double")


def test_int64_range_enforced():
    raw = [make_batch("b", [2**63], [0])]
    with pytest.raises(errors.ValueTypeMismatchError):
        unify(to_kernel(raw), value_type="int64")


# --------------------------------------------------------------- verification

def test_verifier_catches_corrupted_remap():
    """Tampering with a global code must be detected by the oracle."""
    raw = [make_batch("b", ["a", "b"], [0, 1])]
    result = unify(to_kernel(raw), value_type="string")
    r = result.batch_remaps[0]
    tampered = type(result)(
        result.global_dictionary, result.global_value_type,
        result.index_width_bits, result.cardinality, result.sort_policy,
        (type(r)(r.batch_id, r.local_to_global, (1, 0), r.validity,
                 r.row_count, r.null_count),),
        result.stats,
    )
    with pytest.raises(errors.VerificationMismatchError) as ei:
        verify_roundtrip(tampered, to_kernel(raw))
    assert ei.value.details["row"] == 0
