"""Error taxonomy: the four failure kinds must be distinguishable end to end."""

from __future__ import annotations

import pytest

from lightclient.errors import (
    CATEGORY_HTTP_STATUS,
    ErrorCategory,
    ErrorCode,
    _CODE_CATEGORY,
    ComputeFailed,
    InputMalformed,
    NeedCheckpoint,
    ResourceLimit,
    WeightBelowQuorum,
)


def test_every_error_code_has_exactly_one_of_four_categories():
    allowed = {
        ErrorCategory.INPUT,
        ErrorCategory.STATE,
        ErrorCategory.RESOURCE,
        ErrorCategory.COMPUTE,
    }
    for code in ErrorCode:
        cat = _CODE_CATEGORY[code]
        assert cat in allowed

    # representative code -> expected category
    cases = {
        ErrorCode.INPUT_MALFORMED: ErrorCategory.INPUT,
        ErrorCode.SIGNATURE_INVALID: ErrorCategory.INPUT,
        ErrorCode.CHECKPOINT_SIGNATURE_INVALID: ErrorCategory.INPUT,
        ErrorCode.WEIGHT_BELOW_QUORUM: ErrorCategory.STATE,
        ErrorCode.NEED_CHECKPOINT: ErrorCategory.STATE,
        ErrorCode.CONFLICTING_HEADER: ErrorCategory.STATE,
        ErrorCode.UNTRUSTED_BRANCH: ErrorCategory.STATE,
        ErrorCode.STALE_COMMITTEE: ErrorCategory.STATE,
        ErrorCode.RESOURCE_LIMIT: ErrorCategory.RESOURCE,
        ErrorCode.COMPUTE_FAILED: ErrorCategory.COMPUTE,
    }
    for code, cat in cases.items():
        assert _CODE_CATEGORY[code] is cat


def test_categories_map_to_distinct_http_statuses():
    statuses = list(CATEGORY_HTTP_STATUS.values())
    assert len(set(statuses)) == 4  # 400, 409, 413, 500 — all distinct
    assert CATEGORY_HTTP_STATUS[ErrorCategory.INPUT] == 400
    assert CATEGORY_HTTP_STATUS[ErrorCategory.STATE] == 409
    assert CATEGORY_HTTP_STATUS[ErrorCategory.RESOURCE] == 413
    assert CATEGORY_HTTP_STATUS[ErrorCategory.COMPUTE] == 500


def test_error_envelope_shape_is_stable():
    err = WeightBelowQuorum(
        "certificate weight below quorum",
        {"signed_weight": 1, "quorum_weight": 2, "participant_count": 1},
    )
    doc = err.to_dict()
    assert doc["ok"] is False
    assert doc["error"]["code"] == "WEIGHT_BELOW_QUORUM"
    assert doc["error"]["category"] == "state"
    assert doc["error"]["reason"]
    assert doc["error"]["detail"]["signed_weight"] == 1


@pytest.mark.parametrize(
    "exc,code,category,http",
    [
        (InputMalformed("x"), "INPUT_MALFORMED", "input", 400),
        (NeedCheckpoint("x"), "NEED_CHECKPOINT", "state", 409),
        (ResourceLimit("x"), "RESOURCE_LIMIT", "resource", 413),
        (ComputeFailed("x"), "COMPUTE_FAILED", "compute", 500),
    ],
)
def test_four_kinds_are_distinguishable(exc, code, category, http):
    assert exc.code.value == code
    assert exc.category.value == category
    assert CATEGORY_HTTP_STATUS[exc.category] == http


def test_compute_failure_never_silently_other_category():
    # A compute failure is safety-fatal: even carrying state-like detail,
    # its category/status must remain compute/500.
    err = ComputeFailed("signature verification error", {"header": 7})
    assert err.category is ErrorCategory.COMPUTE
    assert CATEGORY_HTTP_STATUS[err.category] == 500
    assert err.to_dict()["error"]["category"] == "compute"
