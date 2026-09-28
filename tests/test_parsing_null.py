"""解析与角色声明：显式 QI/敏感字段、NULL 不被悄悄移出样本。"""

from __future__ import annotations

import pytest

from app.core.errors import FailureCode, ServiceError
from app.core.parsing import parse_dataset
from app.models import NULL_VALUE, DatasetIn
from tests.conftest import load_fixture, make_payload


def _qi(name, values, levels=None):
    levels = levels or [{v: v for v in values}, {v: "ALL" for v in values}]
    return {"name": name, "role": "quasi_identifier", "hierarchy": {"levels": levels}}


def test_quasi_identifier_and_sensitive_must_be_explicit(service, settings):
    # 两列都声明为 insensitive：没有 QI 也没有敏感字段 → 明确拒绝
    payload = DatasetIn(
        name="t",
        columns=[
            {"name": "a", "role": "insensitive"},
            {"name": "b", "role": "insensitive"},
        ],
        rows=[{"a": "1", "b": "2"}],
        k=2,
        l=1,
    )
    with pytest.raises(ServiceError) as ei:
        parse_dataset(payload, settings)
    assert ei.value.code is FailureCode.NO_QUASI_IDENTIFIER


def test_sensitive_required(service, settings):
    payload = DatasetIn(
        name="t",
        columns=[_qi("a", ["1"])],
        rows=[{"a": "1"}],
        k=2,
        l=1,
    )
    with pytest.raises(ServiceError) as ei:
        parse_dataset(payload, settings)
    assert ei.value.code is FailureCode.NO_SENSITIVE


def test_qi_requires_hierarchy(settings):
    # Pydantic 模型层即拒绝：QI 列必须携带 hierarchy
    with pytest.raises(Exception):
        DatasetIn(
            name="t",
            columns=[
                {"name": "a", "role": "quasi_identifier"},
                {"name": "s", "role": "sensitive"},
            ],
            rows=[{"a": "1", "s": "x"}],
            k=2,
            l=1,
        )


def test_null_values_are_retained_as_their_own_class(settings):
    fx = load_fixture("null_patients")
    payload = make_payload(fx, k=2, l=2)
    ds = parse_dataset(payload, settings)

    assert len(ds.rows) == 7  # 没有行被删除
    assert sum(ds.null_row_flags) == 2

    # 原始层：两行 NULL age 必须存在且规范化为哨兵
    null_ages = [r["age"] == NULL_VALUE for r in ds.rows]
    assert sum(null_ages) == 2

    # 警告必须显式说明 NULL 被保留
    assert any("NULL" in w and "remain" in w for w in ds.warnings)


def test_missing_key_treated_as_null_and_retained(settings):
    payload = DatasetIn(
        name="t",
        columns=[_qi("a", ["1", "2"]), {"name": "s", "role": "sensitive"}],
        rows=[{"a": "1"}, {"a": "2", "s": "x"}],  # 第一行缺 s
        k=2,
        l=1,
    )
    ds = parse_dataset(payload, settings)
    assert ds.rows[0]["s"] == NULL_VALUE
    assert len(ds.rows) == 2
    assert any("missing the key" in w for w in ds.warnings)


def test_unknown_row_key_rejected(settings):
    payload = DatasetIn(
        name="t",
        columns=[_qi("a", ["1"]), {"name": "s", "role": "sensitive"}],
        rows=[{"a": "1", "s": "x", "ghost": 9}],
        k=2,
        l=1,
    )
    with pytest.raises(ServiceError) as ei:
        parse_dataset(payload, settings)
    assert ei.value.code is FailureCode.UNKNOWN_COLUMN
    assert "ghost" in ei.value.details["unknown_keys"]


def test_empty_dataset_rejected(settings):
    payload = DatasetIn(
        name="t",
        columns=[_qi("a", ["1"]), {"name": "s", "role": "sensitive"}],
        rows=[],
        k=2,
        l=1,
    )
    with pytest.raises(ServiceError) as ei:
        parse_dataset(payload, settings)
    assert ei.value.code is FailureCode.EMPTY_DATASET


def test_k_below_two_rejected_at_model_layer():
    with pytest.raises(Exception):
        DatasetIn(
            name="t",
            columns=[_qi("a", ["1"]), {"name": "s", "role": "sensitive"}],
            rows=[{"a": "1", "s": "x"}],
            k=1,
            l=1,
        )


def test_l_greater_than_k_rejected():
    with pytest.raises(Exception):
        DatasetIn(
            name="t",
            columns=[_qi("a", ["1", "2"]), {"name": "s", "role": "sensitive"}],
            rows=[{"a": "1", "s": "x"}, {"a": "2", "s": "y"}],
            k=2,
            l=3,
        )
