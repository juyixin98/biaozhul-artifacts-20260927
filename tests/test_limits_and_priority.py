"""资源耗尽（与输入/状态/计算失败区分）及验证前置顺序测试。"""
from __future__ import annotations

import pytest

from merge_engine import MergeRequest

from conftest import cfg, seed

pytestmark = pytest.mark.capture


def test_max_source_rows_is_resource_exhausted(tmp_path):
    eng = seed(tmp_path, "rl", ["k1", "k2"], [])
    req = MergeRequest(
        source={"format": "records",
                "records": [{"k1": f"k{i}", "k2": i} for i in range(5)]},
        config=cfg("rl", ("k1", "k2"), max_source_rows=4),
    )
    result = eng.run(req)
    assert result.status == "REJECTED"
    assert result.error["category"] == "RESOURCE_EXHAUSTED"
    assert result.error["code"] == "PLAN_TOO_LARGE"
    assert result.error["details"]["limit_name"] == "max_source_rows"
    # 早期拒绝：目标表未被写入任何东西
    assert eng.get_target_rows("rl") == []


def test_max_actions_resource_exhausted(tmp_path):
    eng = seed(tmp_path, "rl2", ["k1", "k2"], [])
    req = MergeRequest(
        source={"format": "records",
                "records": [{"k1": f"k{i}", "k2": i} for i in range(3)]},
        config=cfg("rl2", ("k1", "k2"), max_actions=2),
    )
    result = eng.run(req)
    assert result.status == "REJECTED"
    assert result.error["category"] == "RESOURCE_EXHAUSTED"
    assert result.error["details"]["limit_name"] == "max_actions"
    # 计划已决定但验证没过：绝不能有行落库
    assert eng.get_target_rows("rl2") == []
    assert eng.get_actions(result.run_id) == []


def test_max_plan_bytes_resource_exhausted(tmp_path):
    eng = seed(tmp_path, "rl3", ["k1", "k2", "payload"], [])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": "a", "k2": 1, "payload": "x" * 500},
        ]},
        config=cfg("rl3", ("k1", "k2"), max_plan_bytes=100),
    )
    result = eng.run(req)
    # 源字节先超（早期检查）或计划字节超（计划检查）——二者都必须归类 RESOURCE_EXHAUSTED
    assert result.error["category"] == "RESOURCE_EXHAUSTED"
    assert eng.get_target_rows("rl3") == []


def test_error_priority_source_duplicate_before_target_duplicate(tmp_path):
    """源和目标同时脏：源重复键（输入错误）必须优先报告，且不触碰目标判定顺序。"""
    eng = seed(tmp_path, "prio", ["k1", "k2", "v"], [
        {"k1": "t", "k2": 1, "v": 1},
        {"k1": "t", "k2": 1, "v": 2},       # 目标重复
    ])
    req = MergeRequest(
        source={"format": "records", "records": [
            {"k1": "s", "k2": 1, "v": 1},
            {"k1": "s", "k2": 1, "v": 2},   # 源重复
        ]},
        config=cfg("prio", ("k1", "k2")),
    )
    result = eng.run(req)
    assert result.error["code"] == "SOURCE_DUPLICATE_KEY"
    assert result.error["category"] == "INPUT_ERROR"


def test_source_format_error_before_duplicate_check(tmp_path):
    eng = seed(tmp_path, "prio2", ["k1", "k2"], [])
    req = MergeRequest(
        source={"format": "ndjson",
                "content": '{"k1":"a","k2":1}\n{broken\n'},
        config=cfg("prio2", ("k1", "k2")),
    )
    result = eng.run(req)
    assert result.error["category"] == "INPUT_ERROR"
    assert result.error["code"] == "SOURCE_FORMAT_ERROR"


def test_config_error_referencing_unknown_column(tmp_path):
    eng = seed(tmp_path, "prio3", ["k1", "k2"], [])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1}]},
        config=cfg("prio3", ("k1", "k2"), insert={
            "op": "eq", "left": {"side": "source", "column": "ghost"},
            "right": {"literal": 1},
        }),
    )
    result = eng.run(req)
    assert result.status == "REJECTED"
    assert result.error["code"] == "CONFIG_INVALID"


def test_delete_unmatched_requires_explicit_condition(tmp_path):
    eng = seed(tmp_path, "prio4", ["k1", "k2"], [])
    req = MergeRequest(
        source={"format": "records", "records": [{"k1": "a", "k2": 1}]},
        config=cfg("prio4", ("k1", "k2"), delete_unmatched=True),
    )
    result = eng.run(req)
    assert result.error["code"] == "CONFIG_INVALID"
