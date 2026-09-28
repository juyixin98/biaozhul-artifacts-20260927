"""差分引擎：在受限请求空间上穷举两版策略的判定差异。

桶定义（old, new 两版判定）：
- widened（新增允许）: old=DENY 且 new=ALLOW —— 可访问请求集合被扩大
- removed_allow（新增拒绝）: old=ALLOW 且 new=DENY
- new_unknown / removed_unknown: 不确定集合的扩大/消除
- 其余（ALLOW→ALLOW、DENY→DENY、含 UNKNOWN 的其它组合）计入无结论/稳定区。

证据核验：
- 证据必须落在本次分析范围（前缀×操作）内，否则 SCOPE_NO_OVERLAP。
- 每条真实观测与内核判定比对：一致 / 矛盾 / inconclusive（内核 UNKNOWN）。
  矛盾不静默忽略，单独报告并在审计中单列 FAILURE_EVIDENCE。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .audit import Auditor
from .kernel import evaluate
from .models import (
    DiffAnalyzerError,
    FailureKind,
    NoOverlapError,
    Policy,
    Request,
    Verdict,
)
from .universe import MISSING, Region, Universe, build_universe


def _request_json(req: Request) -> dict[str, Any]:
    attrs = {}
    for k, v in req.attributes:
        attrs[k] = v
    return {
        "principal": req.principal,
        "action": req.action,
        "resource": req.resource,
        "attributes": attrs,
    }


@dataclass
class Witness:
    request: dict[str, Any]
    old_verdict: str
    new_verdict: str
    old_decided_by: Optional[str]
    new_decided_by: Optional[str]
    region_anchor: str
    old_default_deny: bool
    new_default_deny: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "old": {
                "verdict": self.old_verdict,
                "decided_by": self.old_decided_by,
                "default_deny": self.old_default_deny,
            },
            "new": {
                "verdict": self.new_verdict,
                "decided_by": self.new_decided_by,
                "default_deny": self.new_default_deny,
            },
            "region_anchor": self.region_anchor,
        }


@dataclass
class EvidenceCheck:
    seq: int
    status: str  # CONSISTENT / CONTRADICTION / INCONCLUSIVE / OUT_OF_SCOPE
    observed: str
    kernel_verdict: Optional[str]
    decided_by: Optional[str]
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "status": self.status,
            "observed": self.observed,
            "kernel_verdict": self.kernel_verdict,
            "decided_by": self.decided_by,
            "detail": self.detail,
        }


@dataclass
class DiffResult:
    diff_id: str
    old_version: str
    new_version: str
    scope: dict[str, Any]
    space: dict[str, Any]
    summary: dict[str, Any]
    witnesses: dict[str, list[dict[str, Any]]]
    evidence_report: Optional[dict[str, Any]]


# 桶键
WIDENED = "widened"                    # 新增允许（核心安全关注）
REMOVED_ALLOW = "removed_allow"        # 新增拒绝
NEW_UNKNOWN = "new_unknown"            # 新引入的不确定
REMOVED_UNKNOWN = "removed_unknown"    # 被消除的不确定


def compute_diff(
    *,
    old_policy: Policy,
    new_policy: Policy,
    scope_prefixes: tuple[str, ...],
    scope_actions: frozenset[str],
    resource_alphabet: list[str],
    configured_principals: list[str],
    include_anonymous: bool,
    max_space_size: int,
    witness_limit: int,
    auditor: Auditor,
    request_id: str,
    actor: str,
    evidence_bundle: Any = None,
) -> DiffResult:
    diff_id = "diff_" + uuid.uuid4().hex

    auditor.event(
        request_id=request_id, actor=actor, component="universe",
        stage="build", summary="构造受限请求空间", diff_id=diff_id,
    )
    universe: Universe = build_universe(
        scope_prefixes=scope_prefixes,
        scope_actions=scope_actions,
        policies=[old_policy, new_policy],
        resource_alphabet=resource_alphabet,
        configured_principals=configured_principals,
        include_anonymous=include_anonymous,
        max_space_size=max_space_size,
    )

    buckets: dict[str, list[Witness]] = {
        WIDENED: [], REMOVED_ALLOW: [], NEW_UNKNOWN: [], REMOVED_UNKNOWN: [],
    }
    # 转移计数（键形如 deny_allow / allow_unknown，含范围外操作点）
    counts: dict[str, int] = {}
    # 四个差分桶只统计分析范围内（前缀区域 × scope 操作）的点
    bucket_counts = {
        WIDENED: 0, REMOVED_ALLOW: 0,
        NEW_UNKNOWN: 0, REMOVED_UNKNOWN: 0,
    }

    enumerated = 0
    for region, req in universe.iter_requests():
        enumerated += 1
        # 仅统计分析范围内的见证：操作必须在 scope_actions；资源区域已保证 in_scope
        in_action_scope = req.action in scope_actions
        old_d = evaluate(old_policy, req)
        new_d = evaluate(new_policy, req)
        o, n = old_d.verdict, new_d.verdict

        key = f"{o.value.lower()}_{n.value.lower()}"
        counts[key] = counts.get(key, 0) + 1

        if not in_action_scope:
            continue

        if o is Verdict.DENY and n is Verdict.ALLOW:
            bucket = WIDENED
        elif o is Verdict.ALLOW and n is Verdict.DENY:
            bucket = REMOVED_ALLOW
        elif o is not Verdict.UNKNOWN and n is Verdict.UNKNOWN:
            bucket = NEW_UNKNOWN
        elif o is Verdict.UNKNOWN and n is not Verdict.UNKNOWN:
            bucket = REMOVED_UNKNOWN
        else:
            bucket = None
        if bucket is not None:
            bucket_counts[bucket] += 1
            w = Witness(
                request=_request_json(req),
                old_verdict=o.value,
                new_verdict=n.value,
                old_decided_by=old_d.decided_by,
                new_decided_by=new_d.decided_by,
                region_anchor=region.anchor,
                old_default_deny=old_d.default_deny,
                new_default_deny=new_d.default_deny,
            )
            if len(buckets[bucket]) < witness_limit:
                buckets[bucket].append(w)

    unknown_total = (
        counts.get("unknown_unknown", 0)
        + counts.get("deny_unknown", 0)
        + counts.get("allow_unknown", 0)
    )

    # 证据核验（可选）
    evidence_report = None
    if evidence_bundle is not None:
        evidence_report = _check_evidence(
            bundle=evidence_bundle,
            policy=new_policy,
            scope_prefixes=scope_prefixes,
            scope_actions=scope_actions,
            auditor=auditor,
            request_id=request_id,
            actor=actor,
            diff_id=diff_id,
        )

    summary = {
        # 不确定优先于一切“好/坏”的确定结论：
        # 只要分析范围内有新增 UNKNOWN，就不能把结果叫成纯粹的 WIDENED。
        "verdict": (
            "WIDENED_WITH_UNKNOWN"
            if bucket_counts[WIDENED] > 0 and bucket_counts[NEW_UNKNOWN] > 0
            else "WIDENED" if bucket_counts[WIDENED] > 0
            else "SHRUNK_WITH_UNKNOWN"
            if bucket_counts[REMOVED_ALLOW] > 0 and bucket_counts[NEW_UNKNOWN] > 0
            else "SHRUNK" if bucket_counts[REMOVED_ALLOW] > 0
            else "UNSURE_UNKNOWN"
            if (bucket_counts[NEW_UNKNOWN] > 0 or unknown_total > 0)
            else "EQUIVALENT"
        ),
        "space_size": universe.size,
        "enumerated": enumerated,
        "transition_counts": counts,
        "widened_requests": bucket_counts[WIDENED],
        "removed_allow_requests": bucket_counts[REMOVED_ALLOW],
        "new_unknown_requests": bucket_counts[NEW_UNKNOWN],
        "removed_unknown_requests": bucket_counts[REMOVED_UNKNOWN],
        "unknown_points_total": unknown_total,
        "has_uncertain_points": bool(
            bucket_counts[NEW_UNKNOWN] > 0 or unknown_total > 0
        ),
        "explicit_deny_precedence": True,
        "default_deny": True,
        "scope": {
            "resource_prefixes": list(scope_prefixes),
            "actions": sorted(scope_actions),
        },
    }

    auditor.event(
        request_id=request_id, actor=actor, component="diffengine",
        stage="enumeration", status="OK", diff_id=diff_id,
        summary=(
            f"穷举 {enumerated}/{universe.size} 点；新增允许 "
            f"{bucket_counts[WIDENED]}，新增拒绝 {bucket_counts[REMOVED_ALLOW]}，"
            f"新增不确定 {bucket_counts[NEW_UNKNOWN]}"
        ),
        detail={"counts": counts, "bucket_counts": bucket_counts},
    )
    if unknown_total:
        auditor.inconclusive(
            request_id=request_id, actor=actor, component="kernel",
            stage="tri-state-evaluation", count=unknown_total, diff_id=diff_id,
        )
    if evidence_report and evidence_report["contradictions"]:
        auditor.failure(
            request_id=request_id, actor=actor, component="evidence",
            stage="reconcile", kind=FailureKind.EVIDENCE_TAMPERED,
            message=f"{evidence_report['contradictions']} 条历史证据与内核判定矛盾",
            diff_id=diff_id,
            detail={"seqs": evidence_report["contradiction_seqs"]},
        )

    return DiffResult(
        diff_id=diff_id,
        old_version=old_policy.version,
        new_version=new_policy.version,
        scope=summary["scope"],
        space={
            "size": universe.size,
            "enumerated": enumerated,
            "regions": [
                {"anchor": r.anchor, "witness": r.witness} for r in universe.regions
            ],
            "principals": [
                ("<anonymous>" if p is None else p) for p in universe.principals
            ],
            "actions": list(universe.actions),
            "attributes": {
                a: [("<missing>" if v is MISSING else v) for v in vals]
                for a, vals in sorted(universe.attribute_domains.items())
            },
            "alphabet": list(universe.alphabet),
        },
        summary=summary,
        witnesses={k: [w.to_dict() for w in v] for k, v in buckets.items()},
        evidence_report=evidence_report,
    )


# ---------------------------------------------------------------------------
# 证据对账
# ---------------------------------------------------------------------------
def _check_evidence(
    *,
    bundle,
    policy: Policy,
    scope_prefixes: tuple[str, ...],
    scope_actions: frozenset[str],
    auditor: Auditor,
    request_id: str,
    actor: str,
    diff_id: str,
) -> dict[str, Any]:
    # 范围交集：证据范围必须与本次分析范围有交叠
    overlap_prefix = any(
        p.startswith(s) or s.startswith(p)
        for p in bundle.scope_prefixes for s in scope_prefixes
    )
    overlap_action = bool(set(bundle.scope_actions) & set(scope_actions))
    checks: list[EvidenceCheck] = []
    if not (overlap_prefix and overlap_action):
        raise NoOverlapError(
            "证据声明范围与本次分析范围（前缀×操作）无交集，无法对账",
            {
                "evidence_scope": {
                    "resource_prefixes": list(bundle.scope_prefixes),
                    "actions": list(bundle.scope_actions),
                },
                "analysis_scope": {
                    "resource_prefixes": list(scope_prefixes),
                    "actions": sorted(scope_actions),
                },
            },
        )

    consistent = contradiction = inconclusive = out_of_scope = 0
    for rec in bundle.records:
        in_prefix = any(rec.resource.startswith(s) for s in scope_prefixes)
        in_action = rec.action in scope_actions
        if not (in_prefix and in_action):
            out_of_scope += 1
            checks.append(EvidenceCheck(
                seq=rec.seq, status="OUT_OF_SCOPE", observed=rec.observed,
                kernel_verdict=None, decided_by=None,
                detail="记录在证据范围内但不在本次分析范围内，未对账",
            ))
            continue
        req = Request.make(rec.principal, rec.action, rec.resource,
                           rec.attributes_dict())
        d = evaluate(policy, req)
        if d.verdict is Verdict.UNKNOWN:
            inconclusive += 1
            checks.append(EvidenceCheck(
                seq=rec.seq, status="INCONCLUSIVE", observed=rec.observed,
                kernel_verdict=d.verdict.value, decided_by=None,
                detail="内核对该请求无法确定（条件含未知值），不据此放行也不判矛盾",
            ))
            continue
        kv = d.verdict.value
        if kv == rec.observed:
            consistent += 1
            checks.append(EvidenceCheck(
                seq=rec.seq, status="CONSISTENT", observed=rec.observed,
                kernel_verdict=kv, decided_by=d.decided_by,
                detail="真实观测与内核判定一致",
            ))
        else:
            contradiction += 1
            checks.append(EvidenceCheck(
                seq=rec.seq, status="CONTRADICTION", observed=rec.observed,
                kernel_verdict=kv, decided_by=d.decided_by,
                detail=(
                    f"主体 {rec.principal or '<anonymous>'} 对 {rec.action} "
                    f"{rec.resource} 的真实观测 {rec.observed} 与内核判定 {kv} 冲突"
                    + (f"（观测规则 {rec.observed_by_rule}）"
                       if rec.observed_by_rule else "")
                ),
            ))

    report = {
        "bundle_id": bundle.bundle_id,
        "policy_version": bundle.policy_version,
        "total": len(bundle.records),
        "consistent": consistent,
        "contradictions": contradiction,
        "contradiction_seqs": [c.seq for c in checks if c.status == "CONTRADICTION"],
        "inconclusive": inconclusive,
        "out_of_scope": out_of_scope,
        "checks": [c.to_dict() for c in checks],
    }
    auditor.event(
        request_id=request_id, actor=actor, component="evidence",
        stage="reconcile",
        status="OK" if contradiction == 0 else "FAILURE_EVIDENCE",
        diff_id=diff_id,
        summary=(
            f"证据对账：一致 {consistent}，矛盾 {contradiction}，"
            f"不确定 {inconclusive}，范围外 {out_of_scope}"
        ),
        detail={"bundle_id": bundle.bundle_id},
    )
    return report
