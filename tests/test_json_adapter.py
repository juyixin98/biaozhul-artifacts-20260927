"""JSON adapter unit tests: wire-level coercion and dedupe canonicalization."""
from __future__ import annotations

import pytest

from app.adapters.jsonio import batch_from_dict
from app.core.errors import (
    InvalidIndexError,
    InvalidValidityError,
    NullDictionaryEntryError,
    ValueTypeMismatchError,
)
from tests.fixtures import make_batch


def test_double_request_coerces_int_entries():
    batch, _ = batch_from_dict(
        make_batch("b", [1, 2.5], [0, 1]), value_type="double", dedupe=False
    )
    assert batch.dictionary == [1.0, 2.5]


def test_bool_dictionary_rejects_int():
    with pytest.raises(ValueTypeMismatchError):
        batch_from_dict(make_batch("b", [1], [0]),
                        value_type="bool", dedupe=False)


def test_null_dictionary_entry_position_reported():
    with pytest.raises(NullDictionaryEntryError) as ei:
        batch_from_dict(make_batch("b", ["a", None], [0, 0]),
                        value_type="string", dedupe=False)
    assert ei.value.details["code"] == 1


def test_dedupe_rewrites_indices_and_reports_pairs(duplicate_dict_batch):
    batch, report = batch_from_dict(
        duplicate_dict_batch, value_type="string", dedupe=True
    )
    assert batch.dictionary == ["x", "y"]
    # codes: 0->0, 1->0, 2->1, 3->0 ; indices [0,1,2,3,2,0]
    assert batch.indices == [0, 0, 1, 0, 1, 0]
    assert report["duplicate_dictionary_entries"] == 2
    assert {p["duplicate_code"] for p in report["duplicate_pairs"]} == {1, 3}


def test_dedupe_disabled_raises_with_first_pair(duplicate_dict_batch):
    from app.core.errors import DuplicateValueInDictionaryError

    with pytest.raises(DuplicateValueInDictionaryError) as ei:
        batch_from_dict(duplicate_dict_batch,
                        value_type="string", dedupe=False)
    assert ei.value.details["first_code"] == 0


def test_validity_defaults_all_true():
    batch, _ = batch_from_dict(make_batch("b", ["a"], [0, 0]),
                               value_type="string", dedupe=False)
    assert batch.validity == [True, True]


def test_invalid_validity_member_type():
    raw = make_batch("b", ["a"], [0], [1])  # int, not bool
    with pytest.raises(InvalidValidityError) as ei:
        batch_from_dict(raw, value_type="string", dedupe=False)
    assert ei.value.details["row"] == 0


def test_float_index_rejected():
    with pytest.raises(InvalidIndexError):
        batch_from_dict(make_batch("b", ["a"], [0.0]),
                        value_type="string", dedupe=False)
