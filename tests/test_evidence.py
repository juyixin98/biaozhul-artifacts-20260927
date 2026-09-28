"""证据：哈希链篡改、签名伪造、范围无交集、矛盾/不一致对账。"""

from __future__ import annotations

import copy

import pytest

from diffanalyzer.evidence import parse_evidence_bundle
from diffanalyzer.models import FailureKind
from diffanalyzer.parser import parse_policy


def _records():
    return [
        {"principal": "acct/alice", "action": "get", "resource": "logs/a",
         "attributes": {"tls": True}, "observed": "ALLOW",
         "observed_by_rule": "read"},
        {"principal": "acct/bob", "action": "get", "resource": "logs/b",
         "attributes": {"tls": True}, "observed": "DENY",
         "observed_by_rule": None},
    ]


def test_valid_bundle_parses_and_recomputes_chain(sign_evidence):
    env = sign_evidence(
        bundle_id="ev1", policy_version="v1",
        scope_prefixes=["logs/"], scope_actions=["get"],
        records=_records())
    bundle = parse_evidence_bundle(env)
    assert len(bundle.records) == 2
    assert bundle.records[0].seq == 1


def test_tampered_record_content_detected_as_evidence_tamper(sign_evidence):
    env = sign_evidence(
        bundle_id="ev1", policy_version="v1",
        scope_prefixes=["logs/"], scope_actions=["get"],
        records=_records())
    # 局部篡改记录内容但保留 record_hash（不重新签名）
    env["signed_at_claim"]["records"][1]["observed"] = "ALLOW"
    with pytest.raises(Exception) as ei:
        parse_evidence_bundle(env)
    assert ei.value.kind is FailureKind.EVIDENCE_TAMPERED
    assert ei.value.details["seq"] == 2


def test_reordering_records_breaks_chain(sign_evidence):
    env = sign_evidence(
        bundle_id="ev1", policy_version="v1",
        scope_prefixes=["logs/"], scope_actions=["get"],
        records=_records())
    recs = env["signed_at_claim"]["records"]
    env["signed_at_claim"]["records"] = list(reversed(recs))
    with pytest.raises(Exception) as ei:
        parse_evidence_bundle(env)
    assert ei.value.kind is FailureKind.EVIDENCE_TAMPERED


def test_forged_signature_is_crypto_failure(sign_evidence, keys):
    from diffanalyzer.crypto_verify import load_private_key, sign
    env = sign_evidence(
        bundle_id="ev1", policy_version="v1",
        scope_prefixes=["logs/"], scope_actions=["get"],
        records=_records())
    forged = sign(load_private_key(keys["other_priv_pem"]),
                  env["signed_at_claim"])
    reg_env = copy.deepcopy(env)
    reg_env["signature"] = forged
    # 链本身没问题……
    bundle = parse_evidence_bundle(reg_env)
    # ……但注册表验签必须失败
    from diffanalyzer.crypto_verify import KeyRegistry
    reg = KeyRegistry.from_pems({"submitter": keys["pub_pem"].decode()})
    with pytest.raises(Exception) as ei:
        reg.verify(bundle.submitted_by, bundle.signed_claim(), bundle.signature)
    assert ei.value.kind is FailureKind.CRYPTO_BAD_SIGNATURE


def test_observed_must_be_concrete_allow_or_deny(sign_evidence):
    # 证据只承载真实确定观测；UNKNOWN 必须在解析期拒绝
    env = sign_evidence(bundle_id="ev1", policy_version="v1",
                        scope_prefixes=["logs/"], scope_actions=["get"],
                        records=[
                            {"principal": "acct/alice", "action": "get",
                             "resource": "logs/a", "attributes": {},
                             "observed": "UNKNOWN"}])
    with pytest.raises(Exception) as ei:
        parse_evidence_bundle(env)
    assert ei.value.kind is FailureKind.SCHEMA_INVALID
    assert "ALLOW/DENY" in ei.value.message


# ---------------------------------------------------------------------------
# 对账：矛盾、不确定、范围无交集
# ---------------------------------------------------------------------------
def _alice_read_logs_policy():
    return parse_policy({"version": "v9", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"],
         "conditions": [{"attribute": "tls", "op": "Eq", "value": True}]}]})


def test_evidence_contradiction_is_reported(service, sign_policy, sign_evidence):
    rid = service.auditor.new_request_id()
    doc = {"version": "v9", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    service.submit_policy(sign_policy(doc), request_id=rid, actor="t")

    env = sign_evidence(
        bundle_id="ev-c", policy_version="v9",
        scope_prefixes=["logs/"], scope_actions=["get"],
        records=[
            # bob 被真实观测为 ALLOW，但策略只允许 alice -> 矛盾
            {"principal": "acct/bob", "action": "get", "resource": "logs/1",
             "attributes": {}, "observed": "ALLOW",
             "observed_by_rule": "some-other-system-rule"}])
    service.submit_evidence(env, request_id=rid, actor="t")

    result = service.run_diff(
        old_version="v9", new_version="v9",
        scope={"resource_prefixes": ["logs/"], "actions": ["get"]},
        request_id=rid, actor="t", evidence_bundle_id="ev-c")
    assert result.evidence_report["contradictions"] == 1
    assert result.evidence_report["contradiction_seqs"] == [1]
    assert result.evidence_report["checks"][0]["status"] == "CONTRADICTION"
    assert "acct/bob" in result.evidence_report["checks"][0]["detail"]


def test_evidence_inconclusive_when_kernel_unknown(service, sign_policy,
                                                   sign_evidence):
    rid = service.auditor.new_request_id()
    doc = {"version": "v9", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"],
         "conditions": [{"attribute": "tls", "op": "Eq", "value": True}]}]}
    service.submit_policy(sign_policy(doc), request_id=rid, actor="t")

    # 真实观测 ALLOW，但证据没带 tls -> 内核无法判定 -> INCONCLUSIVE，不判矛盾
    env = sign_evidence(
        bundle_id="ev-u", policy_version="v9",
        scope_prefixes=["logs/"], scope_actions=["get"],
        records=[{"principal": "acct/alice", "action": "get",
                  "resource": "logs/1", "attributes": {},
                  "observed": "ALLOW", "observed_by_rule": "read"}])
    service.submit_evidence(env, request_id=rid, actor="t")

    from diffanalyzer.diffengine import compute_diff
    from diffanalyzer.evidence import parse_evidence_bundle
    old = service.load_policy("v9")
    result = compute_diff(
        old_policy=old, new_policy=old,
        scope_prefixes=("logs/",), scope_actions=frozenset({"get"}),
        resource_alphabet=service.cfg.resource_alphabet,
        configured_principals=service.cfg.principals,
        include_anonymous=True, max_space_size=service.cfg.max_space_size,
        witness_limit=10, auditor=service.auditor,
        request_id=rid, actor="t",
        evidence_bundle=parse_evidence_bundle(env))
    assert result.evidence_report["contradictions"] == 0
    assert result.evidence_report["inconclusive"] == 1
    assert result.evidence_report["checks"][0]["status"] == "INCONCLUSIVE"


def test_evidence_scope_no_overlap_failure(service, sign_policy, sign_evidence):
    rid = service.auditor.new_request_id()
    doc = {"version": "v9", "rules": [
        {"id": "read", "effect": "ALLOW", "resource_prefix": "logs/",
         "actions": ["get"], "principals": ["acct/alice"]}]}
    service.submit_policy(sign_policy(doc), request_id=rid, actor="t")
    env = sign_evidence(
        bundle_id="ev-s", policy_version="v9",
        scope_prefixes=["metrics/"], scope_actions=["get"],
        records=[{"principal": "acct/alice", "action": "get",
                  "resource": "metrics/1", "attributes": {},
                  "observed": "ALLOW", "observed_by_rule": "read"}])
    service.submit_evidence(env, request_id=rid, actor="t")
    with pytest.raises(Exception) as ei:
        service.run_diff(
            old_version="v9", new_version="v9",
            scope={"resource_prefixes": ["logs/"], "actions": ["get"]},
            request_id=rid, actor="t", evidence_bundle_id="ev-s")
    assert ei.value.kind is FailureKind.SCOPE_NO_OVERLAP
