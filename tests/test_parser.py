"""解析与显式声明的行为测试：失败必须有具体错误码，NULL 不被丢行。"""

from __future__ import annotations

import pytest

from anon_risk.errors import ErrorCode
from anon_risk.kernel.parser import parse_dataset
from anon_risk.kernel.types import NULL


def _base():
    return {
        "columns": ["zip", "age", "disease"],
        "quasi_identifiers": ["zip", "age"],
        "sensitive": ["disease"],
        "rows": [
            ["10001", "23", "Flu"],
            ["10002", "25", "Cold"],
            ["10002", "25", "Cold"],
        ],
        "hierarchies": {
            "zip": {"levels": [{"rule": "prefix", "keep": 4}]},
            "age": {"levels": [{"rule": "range",
                                "bins": [0, 30, 120], "labels": ["<30", "30+"]}]},
        },
    }


def test_explicit_qi_and_sensitive_required():
    p = _base()
    p.pop("quasi_identifiers")
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.EMPTY_QUASI_IDENTIFIERS

    p = _base()
    p.pop("sensitive")
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.EMPTY_SENSITIVE


def test_declared_column_must_exist():
    p = _base()
    p["quasi_identifiers"] = ["zip", "phone"]
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.COLUMN_NOT_FOUND
    assert ei.value.details == {"column": "phone"}


def test_column_role_overlap_rejected():
    p = _base()
    p["sensitive"] = ["zip"]  # zip 同时是 QI
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.DUPLICATE_COLUMN_ROLE


def test_empty_rows_rejected_not_silently_treated_as_success():
    p = _base()
    p["rows"] = []
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.EMPTY_DATA


def test_row_width_mismatch_has_specific_code():
    p = _base()
    p["rows"][1] = ["10002", "25"]  # 缺一列
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.ROW_WIDTH_MISMATCH
    assert ei.value.details["row_index"] == 1
    assert ei.value.details["actual_width"] == 2


def test_null_forms_all_normalized_and_rows_kept():
    p = _base()
    # 空串、纯空白、None 都应规范化为 NULL，行仍保留
    p["rows"] = [
        ["", None, "   "],
        ["10002", "25", "Cold"],
    ]
    ds = parse_dataset(p)
    assert len(ds.rows) == 2
    assert ds.rows[0]["zip"] is NULL
    assert ds.rows[0]["age"] is NULL
    assert ds.rows[0]["disease"] is NULL
    # 解析证据：逐列 NULL 计数
    assert ds.null_counts == {"zip": 1, "age": 1, "disease": 1}


def test_missing_hierarchy_rejected():
    p = _base()
    del p["hierarchies"]["age"]
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.HIERARCHY_MISSING
    assert ei.value.details["column"] == "age"


def test_unknown_rule_rejected():
    p = _base()
    p["hierarchies"]["zip"]["levels"] = [{"rule": "md5"}]
    with pytest.raises(Exception) as ei:
        parse_dataset(p)
    assert ei.value.code is ErrorCode.HIERARCHY_BAD_LEVEL
