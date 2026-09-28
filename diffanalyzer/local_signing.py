"""签名/证据束构造助手：仅供本地夹具、演示 CLI 与测试数据准备使用。

注意：被测安全内核不 import 本模块；参考答案（测试期望）由测试独立给出。
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from .crypto_verify import load_private_key, sign
from .parser import canonical_json_bytes


def signed_policy_envelope(
    private_pem: str | bytes,
    policy_doc: dict[str, Any],
    *,
    submitted_by: str = "submitter",
) -> dict[str, Any]:
    claim = {
        "type": "policy-submission/v1",
        "submitted_by": submitted_by,
        "version": policy_doc["version"],
        "policy": policy_doc,
    }
    key = load_private_key(private_pem)
    return {
        "submitted_by": submitted_by,
        "signed_at_claim": claim,
        "signature": sign(key, claim),
    }


def _chain_base(bundle_id: str, submitted_by: str, policy_version: str,
                scope_prefixes: list[str], scope_actions: list[str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes({
            "bundle_id": bundle_id,
            "submitted_by": submitted_by,
            "policy_version": policy_version,
            "scope": {"resource_prefixes": scope_prefixes,
                      "actions": scope_actions},
        })
    ).hexdigest()


def _record_hash(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(
        prev.encode("ascii") + canonical_json_bytes(body)
    ).hexdigest()


def signed_evidence_envelope(
    private_pem: str | bytes,
    *,
    bundle_id: str,
    policy_version: str,
    scope_prefixes: list[str],
    scope_actions: list[str],
    records: list[dict[str, Any]],
    submitted_by: str = "submitter",
) -> dict[str, Any]:
    """records: [{principal, action, resource, attributes, observed,
                  observed_by_rule}], seq 自动从 1 编号并建哈希链。"""
    base = _chain_base(bundle_id, submitted_by, policy_version,
                       scope_prefixes, scope_actions)
    prev = base
    full_records = []
    for i, r in enumerate(records, start=1):
        body = {
            "seq": i,
            "request": {
                "principal": r.get("principal"),
                "action": r["action"],
                "resource": r["resource"],
                "attributes": r.get("attributes", {}),
            },
            "observed": r["observed"],
            "observed_by_rule": r.get("observed_by_rule"),
        }
        h = _record_hash(prev, body)
        prev = h
        full_records.append({**body, "record_hash": h})

    claim = {
        "type": "evidence-bundle/v1",
        "submitted_by": submitted_by,
        "bundle_id": bundle_id,
        "policy_version": policy_version,
        "scope": {"resource_prefixes": scope_prefixes,
                  "actions": scope_actions},
        "base_hash": base,
        "records": full_records,
    }
    key = load_private_key(private_pem)
    return {
        "submitted_by": submitted_by,
        "signed_at_claim": claim,
        "signature": sign(key, claim),
    }
