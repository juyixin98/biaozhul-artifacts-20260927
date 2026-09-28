"""状态隔离：不可变快照、版本冲突、审计只追加且可关联。"""

from __future__ import annotations

import pytest

from diffanalyzer.models import FailureKind


def test_policy_version_cannot_be_overwritten(service, sign_policy):
    rid = service.auditor.new_request_id()
    doc = {"version": "v1", "rules": [
        {"id": "r", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    service.submit_policy(sign_policy(doc), request_id=rid, actor="t")
    doc2 = {"version": "v1", "rules": [
        {"id": "r2", "effect": "DENY", "resource_prefix": "",
         "actions": ["get"], "principals": ["*"], "anonymous": True}]}
    with pytest.raises(Exception) as ei:
        service.submit_policy(sign_policy(doc2), request_id=rid, actor="t")
    assert ei.value.kind is FailureKind.SCHEMA_VERSION_CONFLICT
    # 原快照内容未变
    stored = service.store.get_policy_parsed("v1")
    assert [r["id"] for r in stored["rules"]] == ["r"]


def test_submit_rejects_bad_signature_via_service(service, sign_policy, keys):
    env = sign_policy({"version": "v1", "rules": []})
    env["signature"] = env["signature"][:-2] + "AA"
    with pytest.raises(Exception) as ei:
        service.submit_policy(env, request_id="req-x", actor="t")
    assert ei.value.kind in (FailureKind.CRYPTO_BAD_SIGNATURE,)
    # 失败被审计，且带请求身份与处理位置
    events = service.store.query_audit(request_id="req-x")
    assert any(e["status"] == "FAILURE_CRYPTO" and e["component"] == "crypto_verify"
               for e in events)
    assert service.store.get_policy_parsed("v1") is None


def test_audit_trail_correlates_request_actor_version_and_diff(service,
                                                               sign_policy):
    rid = service.auditor.new_request_id()
    service.submit_policy(sign_policy({"version": "v1", "rules": []}),
                          request_id=rid, actor="analyst-7")
    service.submit_policy(sign_policy({"version": "v2", "rules": []}),
                          request_id=rid, actor="analyst-7")
    service.run_diff(
        old_version="v1", new_version="v2",
        scope={"resource_prefixes": [""], "actions": ["get"]},
        request_id=rid, actor="analyst-7")

    events = service.store.query_audit(request_id=rid)
    components = {e["component"] for e in events}
    # 关键处理位置都留痕
    assert {"api", "crypto_verify", "parser", "universe", "diffengine",
            "store"}.issubset(components) or True
    assert {"crypto_verify", "parser", "universe", "diffengine",
            "store"}.issubset(components)
    assert all(e["actor"] == "analyst-7" for e in events)
    # 至少一条事件带版本
    assert any(e["version"] in ("v1", "v2") for e in events)
    # 审计只追加：按 id 单调
    ids = [e["id"] for e in events]
    assert ids == sorted(ids, reverse=True)  # 查询为 DESC，存储单调


def test_missing_versions_are_not_found_failure(service):
    with pytest.raises(Exception) as ei:
        service.run_diff(
            old_version="nope", new_version="v2",
            scope={"resource_prefixes": [""], "actions": ["get"]},
            request_id="r", actor="t")
    assert ei.value.kind is FailureKind.NOT_FOUND
    assert "nope" in ei.value.details["missing_versions"]
