"""解析层：严格校验与失败类别断言（不是“接口能调用”）。"""

from __future__ import annotations

import pytest

from diffanalyzer.models import FailureKind
from diffanalyzer.parser import parse_policy


def err_kind(doc):
    with pytest.raises(Exception) as ei:
        parse_policy(doc)
    return ei.value


def test_rejects_unknown_rule_field():
    doc = {"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "a/",
         "actions": ["x"], "principals": [], "admin_override": True}]}
    exc = err_kind(doc)
    assert exc.kind is FailureKind.SCHEMA_INVALID
    assert "未知字段" in exc.message
    assert exc.details["unknown_fields"] == ["admin_override"]


def test_rejects_unknown_condition_op():
    doc = {"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "a/",
         "actions": ["x"], "principals": [],
         "conditions": [{"attribute": "ip", "op": "StartsWith", "value": "10"}]}]}
    exc = err_kind(doc)
    assert exc.kind is FailureKind.SCHEMA_INVALID
    assert "StartsWith" in exc.message


def test_rejects_explicit_unknown_effect():
    doc = {"version": "v1", "rules": [
        {"id": "r1", "effect": "UNKNOWN", "resource_prefix": "",
         "actions": ["x"], "principals": []}]}
    exc = err_kind(doc)
    assert exc.kind is FailureKind.SCHEMA_INVALID


def test_rejects_star_principal_without_anonymous():
    doc = {"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "",
         "actions": ["x"], "principals": ["*"], "anonymous": False}]}
    exc = err_kind(doc)
    assert exc.kind is FailureKind.SCHEMA_INVALID
    assert "anonymous" in exc.message


def test_rejects_bad_cidr_and_glob_charset():
    doc = {"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "",
         "actions": ["x"], "principals": [],
         "conditions": [{"attribute": "ip", "op": "CidrMatch",
                         "value": "999.1.1.0/24"}]}]}
    assert err_kind(doc).kind is FailureKind.SCHEMA_INVALID

    doc2 = {"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "",
         "actions": ["x"], "principals": [],
         "conditions": [{"attribute": "f", "op": "GlobMatch",
                         "value": "[a-z]*"}]}]}
    assert err_kind(doc2).kind is FailureKind.SCHEMA_INVALID


def test_rejects_empty_actions_and_duplicate_ids():
    doc = {"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "",
         "actions": [], "principals": []}]}
    assert err_kind(doc).kind is FailureKind.SCHEMA_INVALID

    doc2 = {"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "",
         "actions": ["x"], "principals": []},
        {"id": "r1", "effect": "DENY", "resource_prefix": "",
         "actions": ["y"], "principals": []}]}
    assert err_kind(doc2).kind is FailureKind.SCHEMA_INVALID
    assert err_kind(doc2).details == {"duplicates": ["r1"]}


def test_prefix_normalization_closes_segment_boundary():
    # "logs" 必须规范化成 "logs/"，否则会静默匹配 "logs-secret/x"
    p = parse_policy({"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "logs",
         "actions": ["x"], "principals": []}]})
    assert p.rules[0].resource_prefix == "logs/"


def test_eq_is_type_strict():
    p = parse_policy({"version": "v1", "rules": [
        {"id": "r1", "effect": "ALLOW", "resource_prefix": "",
         "actions": ["x"], "principals": ["*"], "anonymous": True,
         "conditions": [{"attribute": "n", "op": "Eq", "value": 1}]}]})
    from diffanalyzer.kernel import evaluate
    from diffanalyzer.models import Request, Verdict
    req_str = Request.make(None, "x", "", {"n": "1"})
    req_bool = Request.make(None, "x", "", {"n": True})
    req_int = Request.make(None, "x", "", {"n": 1})
    req_float = Request.make(None, "x", "", {"n": 1.0})
    # 不可比类型 -> UNKNOWN（绝不按“不等”而默认允许或草率拒绝）
    assert evaluate(p, req_str).verdict is Verdict.UNKNOWN
    assert evaluate(p, req_bool).verdict is Verdict.UNKNOWN
    # int 与 float 仍视为不同类型（避免 1 == 1.0 的静默跨型）
    assert evaluate(p, req_float).verdict is Verdict.UNKNOWN
    assert evaluate(p, req_int).verdict is Verdict.ALLOW
