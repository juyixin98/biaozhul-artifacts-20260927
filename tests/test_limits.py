"""资源耗尽与输入限制测试：区分“输入太大/程序过大/命中过多”等失败类别。"""
from __future__ import annotations

import pytest

from app import config, engine, planning
from app.errors import (
    PayloadTooLargeError,
    RegexProgramTooLargeError,
    ResourceExhaustedError,
)
from app.schemas import RuleIn


def test_text_too_large_is_413_input_category(record, monkeypatch):
    monkeypatch.setattr(config, "LIMITS", config.Limits(max_text_chars=10))
    # planning 直接读取 app.config.LIMITS
    monkeypatch.setattr(planning, "LIMITS", config.Limits(max_text_chars=10))
    with pytest.raises(PayloadTooLargeError) as ei:
        planning.build_plan("x" * 11, [RuleIn(rule_id="r", pattern="x", template="y")])
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.http_status == 413
    assert ei.value.details["limit"] == 10


def test_match_budget_exhaustion_is_resource_category(record, monkeypatch):
    # 空模式在 100 字符上产生 101 命中，预算 50 -> 资源耗尽
    limits = config.Limits(max_matches_per_plan=50)
    monkeypatch.setattr(planning, "LIMITS", limits)
    with pytest.raises(ResourceExhaustedError) as ei:
        planning.build_plan("a" * 100, [RuleIn(rule_id="z", pattern="", template="|")])
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "RESOURCE_EXHAUSTED"
    assert ei.value.category == "resource"
    assert ei.value.details["limit"] == 50


def test_rule_count_budget_is_input_category(record, monkeypatch):
    limits = config.Limits(max_rules_per_plan=2)
    monkeypatch.setattr(planning, "LIMITS", limits)
    rules = [RuleIn(rule_id=f"r{i}", pattern="x", template="y") for i in range(3)]
    with pytest.raises(Exception) as ei:
        planning.build_plan("xxx", rules)
    assert ei.value.code == "INPUT_INVALID_RULE"
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)


def test_pattern_too_long_rejected_by_schema(record):
    # pydantic 层长度限制 -> 422 输入错误（API 集成在 test_api 体现）
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RuleIn(rule_id="r", pattern="x" * (config.LIMITS.max_pattern_chars + 1), template="y")


def test_program_too_large_distinct_from_syntax_error(record, monkeypatch):
    # 合法语法但超过引擎程序预算：COMPUTE_REGEX_PROGRAM_TOO_LARGE（413），
    # 与 COMPUTE_REGEX_COMPLEX 语法错误可区分
    monkeypatch.setattr(engine, "LIMITS", config.Limits(regex_mem_budget=512))
    with pytest.raises(RegexProgramTooLargeError) as ei:
        engine.compile_pattern("(?:a|b|c|d|e|f|g|h)" * 30, "")
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "COMPUTE_REGEX_PROGRAM_TOO_LARGE"
    assert ei.value.http_status == 413


@pytest.mark.parametrize(
    "size_field,value",
    [("max_text_chars", 5_000_000), ("regex_mem_budget", 1 << 20),
     ("max_matches_per_plan", 200_000), ("max_rules_per_plan", 200)],
)
def test_default_limits_are_present(size_field, value):
    assert getattr(config.LIMITS, size_field) == value
