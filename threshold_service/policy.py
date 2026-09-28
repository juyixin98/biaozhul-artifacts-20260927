"""规则/证据解析（policy）：把提交的份额信封解析、分类成判定。

本模块是**纯判定层**：不做插值、不读秘密，只回答——
"这些证据中哪些可用、为什么其余被拒、当前能否进入恢复"。

失败类别（细粒度，测试对每一类做具体断言）：
- MALFORMED_EVIDENCE      信封无法解析（坏 JSON / 缺字段 / 类型错 / base64 错）
- MIXED_SET               一次提交引用了多个集合身份（硬拒绝）
- UNKNOWN_SET             集合身份在服务端无记录（硬拒绝）
- FIELD_INCOMPATIBLE      份额字段参数与集合记录不一致
- THRESHOLD_MISMATCH      份额门限与集合记录不一致
- BAD_TAG                 独立完整性标签校验失败（被篡改或非本服务签发）
- BAD_LENGTH              y 长度与集合秘密长度不一致
- DUPLICATE_X_CONFLICT    同一 x 出现不同份额（无法安全选择，两份均排除）
- BELOW_THRESHOLD         去重后的可用横坐标数不足门限（硬拒绝）
- COMMIT_MISMATCH         恢复候选值未通过承诺（在内核中判为无法判定）

"重复横坐标不重复计数"：完全相同的份额重复提交只计一次（指纹去重）。
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field

from .integrity import verify_share_tag
from .models import MalformedEnvelope, ShareEnvelope, canonical_envelope_bytes, envelope_fingerprint

# 结果
READY = "READY"
REJECTED = "REJECTED"

# 失败类别
MALFORMED_EVIDENCE = "MALFORMED_EVIDENCE"
MIXED_SET = "MIXED_SET"
UNKNOWN_SET = "UNKNOWN_SET"
FIELD_INCOMPATIBLE = "FIELD_INCOMPATIBLE"
THRESHOLD_MISMATCH = "THRESHOLD_MISMATCH"
BAD_TAG = "BAD_TAG"
BAD_LENGTH = "BAD_LENGTH"
DUPLICATE_X_CONFLICT = "DUPLICATE_X_CONFLICT"
BELOW_THRESHOLD = "BELOW_THRESHOLD"
COMMIT_MISMATCH = "COMMIT_MISMATCH"


def raw_fingerprint(raw: str) -> str:
    """无法解析的证据也用指纹（对原始字节哈希）进日志，绝不打印原文。"""
    return "raw:sha256:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class _Parsed:
    index: int
    raw_fp: str
    envelope: ShareEnvelope | None = None
    fp: str | None = None
    malformed_reason: str | None = None


@dataclass
class Evaluation:
    outcome: str  # READY / REJECTED
    category: str | None
    reason: str
    set_id: str | None
    threshold: int | None
    field: dict | None
    submitted_count: int
    distinct_x_count: int = 0
    usable: list[tuple[ShareEnvelope, bytes]] = field(default_factory=list)
    # 诊断桶（全部装指纹 / 标识，不含敏感明文）
    malformed: list[dict] = field(default_factory=list)
    bad_tag_fingerprints: list[str] = field(default_factory=list)
    field_mismatches: list[str] = field(default_factory=list)
    threshold_mismatches: list[str] = field(default_factory=list)
    bad_length_fingerprints: list[str] = field(default_factory=list)
    duplicate_conflict_fingerprints: list[str] = field(default_factory=list)
    repeated_fingerprints: list[str] = field(default_factory=list)
    accepted_fingerprints: list[str] = field(default_factory=list)
    unknown_sets: list[str] = field(default_factory=list)

    def audit_detail(self) -> dict:
        return {
            "set_id": self.set_id,
            "threshold": self.threshold,
            "field": self.field,
            "submitted_count": self.submitted_count,
            "distinct_x_count": self.distinct_x_count,
            "distinct_accepted_fingerprints": len(self.accepted_fingerprints),
            "malformed": self.malformed,
            "bad_tag_fingerprints": self.bad_tag_fingerprints,
            "field_mismatches": self.field_mismatches,
            "threshold_mismatches": self.threshold_mismatches,
            "bad_length_fingerprints": self.bad_length_fingerprints,
            "duplicate_conflict_fingerprints": self.duplicate_conflict_fingerprints,
            "repeated_fingerprints": self.repeated_fingerprints,
            "accepted_fingerprints": self.accepted_fingerprints,
            "unknown_sets": self.unknown_sets,
            "reason": self.reason,
        }


def evaluate(raw_envelopes: list[str], repository, master_key: bytes) -> Evaluation:
    """按固定顺序解析并分类证据；不满足安全前提即给出明确拒绝类别。"""
    submitted = len(raw_envelopes)
    parsed: list[_Parsed] = []
    for index, raw in enumerate(raw_envelopes):
        item = _Parsed(index=index, raw_fp=raw_fingerprint(raw))
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise MalformedEnvelope("evidence must be a JSON object")
            env = ShareEnvelope.from_dict(data)
        except (MalformedEnvelope, ValueError, TypeError) as exc:
            item.malformed_reason = str(exc)[:200]
        else:
            item.envelope = env
            item.fp = envelope_fingerprint(env)
        parsed.append(item)

    malformed = [
        {"index": p.index, "fingerprint": p.raw_fp, "reason": p.malformed_reason}
        for p in parsed
        if p.envelope is None
    ]
    well_formed = [p for p in parsed if p.envelope is not None]

    def reject(category: str, reason: str, *, set_id=None, threshold=None,
               field=None, **extra) -> Evaluation:
        ev = Evaluation(
            outcome=REJECTED, category=category, reason=reason, set_id=set_id,
            threshold=threshold, field=field, submitted_count=submitted,
            malformed=malformed, **extra,
        )
        return ev

    if not well_formed:
        return reject(MALFORMED_EVIDENCE,
                      "no parseable share envelopes in submission")

    # --- 集合身份：硬一致性 ---
    set_ids = {p.envelope.set_id for p in well_formed}
    if len(set_ids) > 1:
        ev = Evaluation(
            outcome=REJECTED, category=MIXED_SET,
            reason=("submission binds shares to more than one set identity; "
                    "cross-set recovery is refused by policy"),
            set_id=sorted(set_ids)[0], threshold=None, field=None,
            submitted_count=submitted, malformed=malformed,
        )
        # 借 unknown_sets 桶记录所见集合（诊断需要），语义在 reason 中说明。
        ev.unknown_sets = sorted(set_ids)
        return ev

    set_id = next(iter(set_ids))
    set_row = repository.get_set(set_id)
    if set_row is None:
        return reject(UNKNOWN_SET, f"set {set_id!r} is not registered on this server",
                      set_id=set_id, unknown_sets=[set_id])

    threshold = set_row["threshold"]
    field_dict = {"bits": set_row["field_bits"], "generator": set_row["field_gen"]}
    secret_len = set_row["secret_len"]

    ev = Evaluation(
        outcome=READY, category=None, reason="", set_id=set_id,
        threshold=threshold, field=field_dict, submitted_count=submitted,
        malformed=malformed,
    )

    # --- 逐条证据校验（排除项不进入可用集） ---
    for p in well_formed:
        env = p.envelope
        if env.field != field_dict:
            ev.field_mismatches.append(p.fp)
            continue
        if env.threshold != threshold:
            ev.threshold_mismatches.append(p.fp)
            continue
        try:
            tag_ok = verify_share_tag(
                master_key, canonical_envelope_bytes(env), base64.b64decode(env.tag)
            )
        except Exception:
            tag_ok = False
        if not tag_ok:
            ev.bad_tag_fingerprints.append(p.fp)
            continue
        if len(env.y_bytes()) != secret_len:
            ev.bad_length_fingerprints.append(p.fp)
            continue

        # 完全相同的份额（同指纹）重复提交：只计一次。
        if any(fp == p.fp for fp in ev.accepted_fingerprints):
            ev.repeated_fingerprints.append(p.fp)
            continue
        if any(x == env.x for x, _ in [(e.x, None) for e, _ in ev.usable]):
            # 同 x 不同内容且都通过了标签：无法安全选择，冲突双方都排除。
            prior = next(e for e, _ in ev.usable if e.x == env.x)
            prior_fp = envelope_fingerprint(prior)
            for fp in (prior_fp, p.fp):
                ev.duplicate_conflict_fingerprints.append(fp)
            ev.usable = [(e, y) for e, y in ev.usable if e.x != env.x]
            ev.accepted_fingerprints = [
                fp for fp in ev.accepted_fingerprints if fp != prior_fp
            ]
            continue

        ev.usable.append((env, env.y_bytes()))
        ev.accepted_fingerprints.append(p.fp)

    ev.distinct_x_count = len(ev.usable)
    if ev.distinct_x_count < threshold:
        ev.outcome = REJECTED
        ev.category = BELOW_THRESHOLD
        ev.reason = (
            f"{ev.distinct_x_count} distinct authenticated x < threshold {threshold}; "
            "duplicate x values are not counted twice"
        )
        return ev

    ev.reason = f"{ev.distinct_x_count} distinct authenticated shares meet threshold {threshold}"
    return ev
