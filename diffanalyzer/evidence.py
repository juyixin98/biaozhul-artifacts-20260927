"""历史证据（真实请求/决策记录）解析与完整性核验。

证据信封同样用 Ed25519 签名；此外每条记录参与一条重算哈希链，
使得“整条重签”与“局部篡改”能被区分成不同失败类别：
- 签名无效/缺签/未知公钥 -> CRYPTO_*
- 签名有效但链重算不一致   -> EVIDENCE_TAMPERED（给出被篡改的 seq）

证据记录只能承载确定的真实观测（ALLOW/DENY）；
内核对同一请求给 UNKNOWN 时记为 inconclusive（不确定），不算一致也不算矛盾。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import EvidenceError, SchemaError
from .parser import canonical_json_bytes, source_hash
import hashlib


@dataclass(frozen=True)
class EvidenceRecord:
    seq: int
    principal: Optional[str]
    action: str
    resource: str
    attributes: tuple[tuple[str, Any], ...]
    observed: str  # ALLOW / DENY
    observed_by_rule: Optional[str]
    record_hash: str

    def attributes_dict(self) -> dict[str, Any]:
        return dict(self.attributes)


@dataclass(frozen=True)
class EvidenceBundle:
    bundle_id: str
    submitted_by: str
    scope_prefixes: tuple[str, ...]
    scope_actions: tuple[str, ...]
    policy_version: str
    records: tuple[EvidenceRecord, ...]
    base_hash: str
    signature: str
    doc: dict[str, Any] = field(default_factory=dict)

    def signed_claim(self) -> dict[str, Any]:
        # 与提交内容一致的验签对象（不含 envelope 外层签名字段）
        return {
            "type": "evidence-bundle/v1",
            "submitted_by": self.submitted_by,
            "bundle_id": self.bundle_id,
            "policy_version": self.policy_version,
            "scope": {
                "resource_prefixes": list(self.scope_prefixes),
                "actions": list(self.scope_actions),
            },
            "base_hash": self.base_hash,
            "records": [
                {
                    "seq": r.seq,
                    "request": {
                        "principal": r.principal,
                        "action": r.action,
                        "resource": r.resource,
                        "attributes": dict(r.attributes),
                    },
                    "observed": r.observed,
                    "observed_by_rule": r.observed_by_rule,
                    "record_hash": r.record_hash,
                }
                for r in self.records
            ],
        }


def _prefix_norm(p: str) -> str:
    if p == "":
        return ""
    return p if p.endswith("/") else p + "/"


def _chain_base(bundle_id: str, submitted_by: str, policy_version: str,
                scope_prefixes: list[str], scope_actions: list[str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "bundle_id": bundle_id,
                "submitted_by": submitted_by,
                "policy_version": policy_version,
                "scope": {
                    "resource_prefixes": scope_prefixes,
                    "actions": scope_actions,
                },
            }
        )
    ).hexdigest()


def _record_hash(prev: str, record_body: dict[str, Any]) -> str:
    return hashlib.sha256(
        prev.encode("ascii") + canonical_json_bytes(record_body)
    ).hexdigest()


def parse_evidence_bundle(envelope: dict[str, Any]) -> EvidenceBundle:
    """解析证据信封并*重算*哈希链（不在此处验签，验签由服务层用注册表做）。"""
    if not isinstance(envelope, dict):
        raise SchemaError("证据信封必须是对象")
    claim = envelope.get("signed_at_claim")
    signature = envelope.get("signature")
    if not isinstance(claim, dict) or not isinstance(signature, str):
        raise SchemaError("证据信封缺少 signed_at_claim 或 signature")
    if claim.get("type") != "evidence-bundle/v1":
        raise SchemaError("不支持的证据声明类型", {"type": claim.get("type")})

    bundle_id = claim.get("bundle_id")
    submitted_by = claim.get("submitted_by")
    policy_version = claim.get("policy_version")
    scope = claim.get("scope")
    raw_records = claim.get("records")
    base_hash_claim = claim.get("base_hash")
    if not all(isinstance(x, str) and x
               for x in (bundle_id, submitted_by, policy_version)):
        raise SchemaError("bundle_id/submitted_by/policy_version 必须为非空字符串")
    if not isinstance(scope, dict):
        raise SchemaError("证据 scope 必须是对象")
    prefixes = scope.get("resource_prefixes")
    actions = scope.get("actions")
    if not isinstance(prefixes, list) or not all(isinstance(p, str) for p in prefixes):
        raise SchemaError("证据 scope.resource_prefixes 必须是字符串列表")
    if not prefixes:
        raise SchemaError("证据 scope.resource_prefixes 不能为空")
    if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
        raise SchemaError("证据 scope.actions 必须是字符串列表")
    if not actions:
        raise SchemaError("证据 scope.actions 不能为空")
    if len(set(actions)) != len(actions):
        raise SchemaError("证据 scope.actions 含重复项")
    prefixes = [_prefix_norm(p) for p in prefixes]
    if len(set(prefixes)) != len(prefixes):
        raise SchemaError("证据 scope.resource_prefixes 规范化后重复")
    if not isinstance(raw_records, list) or not raw_records:
        raise SchemaError("证据 records 必须是非空列表")

    base = _chain_base(bundle_id, submitted_by, policy_version, prefixes, actions)
    if not isinstance(base_hash_claim, str) or base_hash_claim != base:
        # base_hash 是链的一部分，改动它即篡改
        raise EvidenceError(
            "证据 base_hash 重算不一致（信封元数据可能被篡改）",
            {"expected": base, "claimed": base_hash_claim},
        )

    prev = base
    seqs_seen: set[int] = set()
    records: list[EvidenceRecord] = []
    for i, item in enumerate(raw_records):
        if not isinstance(item, dict):
            raise SchemaError(f"records[{i}] 必须是对象")
        seq = item.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise SchemaError(f"records[{i}].seq 必须是 >=1 的整数")
        if seq in seqs_seen:
            raise SchemaError(f"records seq 重复: {seq}")
        seqs_seen.add(seq)

        request = item.get("request")
        if not isinstance(request, dict):
            raise SchemaError(f"records[{i}].request 必须是对象")
        principal = request.get("principal")
        if principal is not None and not isinstance(principal, str):
            raise SchemaError(f"records[{i}] principal 必须是字符串或 null")
        action = request.get("action")
        resource = request.get("resource")
        attributes = request.get("attributes", {})
        if not isinstance(action, str) or not action:
            raise SchemaError(f"records[{i}] action 必须是非空字符串")
        if not isinstance(resource, str):
            raise SchemaError(f"records[{i}] resource 必须是字符串（可为空串）")
        if not isinstance(attributes, dict):
            raise SchemaError(f"records[{i}] attributes 必须是对象")
        for k, v in attributes.items():
            if not isinstance(k, str):
                raise SchemaError(f"records[{i}] 属性名必须是字符串")
            if isinstance(v, (dict, list)) or v is None:
                raise SchemaError(
                    f"records[{i}] 属性 {k!r} 必须是标量（缺失请省略该键）"
                )

        observed = item.get("observed")
        if observed not in ("ALLOW", "DENY"):
            raise SchemaError(
                f"records[{i}] observed 只能是真实确定判定 ALLOW/DENY",
                {"got": observed},
            )
        observed_by_rule = item.get("observed_by_rule")
        if observed_by_rule is not None and not isinstance(observed_by_rule, str):
            raise SchemaError(f"records[{i}] observed_by_rule 必须是字符串或 null")

        body = {
            "seq": seq,
            "request": {
                "principal": principal,
                "action": action,
                "resource": resource,
                "attributes": attributes,
            },
            "observed": observed,
            "observed_by_rule": observed_by_rule,
        }
        expected_hash = _record_hash(prev, body)
        claimed_hash = item.get("record_hash")
        if claimed_hash != expected_hash:
            raise EvidenceError(
                f"证据记录 seq={seq} 哈希链重算不一致（记录被篡改或顺序被改动）",
                {"seq": seq, "expected": expected_hash, "claimed": claimed_hash},
            )
        prev = expected_hash
        records.append(
            EvidenceRecord(
                seq=seq,
                principal=principal,
                action=action,
                resource=resource,
                attributes=tuple(sorted(attributes.items())),
                observed=observed,
                observed_by_rule=observed_by_rule,
                record_hash=expected_hash,
            )
        )

    if seqs_seen != set(range(1, len(records) + 1)):
        raise SchemaError(
            "证据 records 的 seq 必须从 1 连续编号",
            {"seqs": sorted(seqs_seen)},
        )

    return EvidenceBundle(
        bundle_id=bundle_id,
        submitted_by=submitted_by,
        scope_prefixes=tuple(prefixes),
        scope_actions=tuple(sorted(actions)),
        policy_version=policy_version,
        records=tuple(records),
        base_hash=base,
        signature=signature,
        doc=envelope,
    )
