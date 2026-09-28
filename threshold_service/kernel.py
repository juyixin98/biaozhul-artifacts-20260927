"""安全内核：编排分片、证据判定与恢复，并给出可审计结论。

恢复语义（重要）：
- 所有通过策略层的份额一起插值；若候选秘密通过承诺 => ACCEPTED。
- 未通过承诺 => INDETERMINATE（COMMIT_MISMATCH），绝不输出错误秘密。
  此时做**尽力而为**的小子集枚举，找出能恢复出承诺匹配秘密的门限子集，
  仅作排查线索——恢复失败本身**不能定位全部恶意参与者**：
  多个同谋、份额数过大、或枚举预算超限时都可能无法归因。
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from itertools import combinations

from .audit import (
    AuditLogger,
    OUTCOME_ACCEPTED,
    OUTCOME_INDETERMINATE,
    OUTCOME_REJECTED,
)
from .gf import FieldParams
from .integrity import (
    secret_commitment,
    tag_share,
    verify_commitment,
)
from .models import ENVELOPE_VERSION, ShareEnvelope, b64e, canonical_envelope_bytes, envelope_fingerprint
from .policy import (
    COMMIT_MISMATCH,
    Evaluation,
    evaluate,
)
from .repository import Repository
from .shamir import MAX_SHARES, ShareError, interpolate_at_zero, split_secret

SUBSET_ENUM_BUDGET = 256  # 最多枚举的门限子集数（防组合爆炸）


def secret_fp(secret: bytes) -> str:
    """秘密指纹（仅用于审计，不进承诺判定）。"""
    return "sha256:" + hashlib.sha256(secret).hexdigest()[:16]


@dataclass
class IssueResult:
    set_id: str
    threshold: int
    share_count: int
    field: dict
    commitment: str
    secret_fp: str
    shares: list[dict]
    labels: dict[str, str]


@dataclass
class RecoverResult:
    outcome: str  # ACCEPTED / REJECTED / INDETERMINATE
    category: str | None
    request_id: str
    set_id: str | None
    reason: str
    evaluation: Evaluation
    secret_b64: str | None = None
    secret_fp: str | None = None
    # 尽力而为的归因线索（不作为完整指控）
    consistent_subsets: list[list[str]] = field(default_factory=list)
    always_good_x: list[int] = field(default_factory=list)
    enum_truncated: bool = False


class SecurityKernel:
    def __init__(self, settings, repository: Repository, audit: AuditLogger):
        self.settings = settings
        self.repo = repository
        self.audit = audit

    # ------------------------------------------------------------------ 分片
    def issue_set(
        self,
        *,
        secret: bytes,
        threshold: int,
        share_count: int,
        labels: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> IssueResult:
        request_id = request_id or f"req_{secrets.token_hex(8)}"
        params = FieldParams()
        labels = labels or {}

        # 输入校验（明确失败类别前置，避免落库半截状态）。
        if threshold < 2 or share_count < threshold or share_count > MAX_SHARES:
            self.audit.record(
                request_id=request_id, stage="issue", outcome=OUTCOME_REJECTED,
                reason="invalid threshold/share_count",
                detail={"threshold": threshold, "share_count": share_count},
            )
            raise ShareError(
                f"require 2 <= threshold <= share_count <= {MAX_SHARES}"
            )
        if not isinstance(secret, (bytes, bytearray)) or not secret:
            self.audit.record(
                request_id=request_id, stage="issue", outcome=OUTCOME_REJECTED,
                reason="empty secret",
            )
            raise ShareError("secret must be non-empty bytes")

        set_id = secrets.token_hex(8)
        while self.repo.get_set(set_id) is not None:
            set_id = secrets.token_hex(8)

        points = split_secret(secret, threshold, share_count, params)
        commitment = secret_commitment(self.settings.master_key, set_id, secret)
        self.repo.save_set(
            set_id=set_id, threshold=threshold,
            field_bits=params.bits, field_gen=params.generator,
            secret_len=len(secret), commitment=commitment,
            secret_fp=secret_fp(secret), labels=labels,
        )

        envelopes: list[dict] = []
        label_by_x: dict[str, str] = {}
        for idx, (x, y) in enumerate(points):
            env = ShareEnvelope(
                version=ENVELOPE_VERSION, set_id=set_id, x=x, y=b64e(y),
                threshold=threshold, field=params.to_dict(), tag="",
            )
            tag = tag_share(self.settings.master_key, canonical_envelope_bytes(env))
            tagged = ShareEnvelope(
                version=env.version, set_id=env.set_id, x=env.x, y=env.y,
                threshold=env.threshold, field=env.field, tag=b64e(tag),
            )
            fp = envelope_fingerprint(tagged)
            label = labels.get(str(idx + 1))
            self.repo.save_share(
                set_id=set_id, x=x, fingerprint=fp, label=label,
                envelope=tagged.to_dict(),
            )
            envelopes.append(tagged.to_dict())
            if label:
                label_by_x[str(x)] = label

        self.audit.record(
            request_id=request_id, stage="issue", outcome=OUTCOME_ACCEPTED,
            reason=f"issued {share_count} shares under threshold {threshold}",
            detail={
                "set_id": set_id, "threshold": threshold,
                "field": params.canonical(), "share_count": share_count,
                "secret_fp": secret_fp(secret),
            },
        )
        return IssueResult(
            set_id=set_id, threshold=threshold, share_count=share_count,
            field=params.to_dict(), commitment=commitment,
            secret_fp=secret_fp(secret), shares=envelopes, labels=label_by_x,
        )

    # ------------------------------------------------------------------ 恢复
    def recover(self, raw_envelopes: list[str], *, request_id: str | None = None) -> RecoverResult:
        request_id = request_id or f"req_{secrets.token_hex(8)}"
        evaluation = evaluate(raw_envelopes, self.repo, self.settings.master_key)

        if evaluation.outcome == "REJECTED":
            self.audit.record(
                request_id=request_id, stage="recover.policy",
                outcome=OUTCOME_REJECTED, reason=evaluation.reason,
                detail=evaluation.audit_detail(),
            )
            return RecoverResult(
                outcome=OUTCOME_REJECTED, category=evaluation.category,
                request_id=request_id, set_id=evaluation.set_id,
                reason=evaluation.reason, evaluation=evaluation,
            )

        params = FieldParams.from_dict(evaluation.field)
        set_row = self.repo.get_set(evaluation.set_id)
        commitment = set_row["commitment"]

        points = [(env.x, y) for env, y in evaluation.usable]
        candidate = interpolate_at_zero(points, params)

        detail = evaluation.audit_detail()
        detail.update({
            "expected_secret_len": set_row["secret_len"],
            "candidate_secret_fp": secret_fp(candidate),
        })

        if verify_commitment(self.settings.master_key, evaluation.set_id, candidate, commitment):
            self.audit.record(
                request_id=request_id, stage="recover", outcome=OUTCOME_ACCEPTED,
                reason="candidate secret verified against stored commitment",
                detail=detail,
            )
            return RecoverResult(
                outcome=OUTCOME_ACCEPTED, category=None, request_id=request_id,
                set_id=evaluation.set_id,
                reason="threshold met; reconstructed secret matches the set commitment",
                evaluation=evaluation, secret_b64=b64e(candidate),
                secret_fp=secret_fp(candidate),
            )

        # 承诺失败：插值结果不可信。做尽力而为的小子集枚举。
        consistent, trunc = self._enumerate_consistent(
            evaluation.usable, params, evaluation.threshold,
            evaluation.set_id, commitment,
        )
        always_good = self._always_present(consistent)
        self.audit.record(
            request_id=request_id, stage="recover", outcome=OUTCOME_INDETERMINATE,
            reason="reconstructed candidate failed commitment; cannot certify the secret "
                   "or identify every malicious share",
            detail={
                **detail,
                "accepted_fingerprints": [
                    envelope_fingerprint(env) for env, _ in evaluation.usable
                ],
                "committed": False,
            },
        )
        return RecoverResult(
            outcome=OUTCOME_INDETERMINATE, category=COMMIT_MISMATCH,
            request_id=request_id, set_id=evaluation.set_id,
            reason=(
                "shares passed individual authentication but interpolation disagrees "
                "with the secret commitment; recovery refused. Subset diagnostics are "
                "best-effort and do NOT constitute identification of all bad parties"
            ),
            evaluation=evaluation,
            consistent_subsets=consistent[:20],
            always_good_x=always_good,
            enum_truncated=trunc,
        )

    def _enumerate_consistent(
        self, usable: list, params: FieldParams, threshold: int,
        set_id: str, commitment: str,
    ) -> tuple[list[list[str]], bool]:
        """枚举门限子集，记录通过承诺者；受 SUBSET_ENUM_BUDGET 限制。"""
        by_x = {env.x: (env, y) for env, y in usable}
        good: list[list[str]] = []
        truncated = False
        count = 0
        for combo in combinations(sorted(by_x), threshold):
            count += 1
            if count > SUBSET_ENUM_BUDGET:
                truncated = True
                break
            points = [(x, by_x[x][1]) for x in combo]
            try:
                candidate = interpolate_at_zero(points, params)
            except Exception:
                continue
            if verify_commitment(self.settings.master_key, set_id, candidate, commitment):
                good.append([f"x={x}:{envelope_fingerprint(by_x[x][0])}" for x in combo])
        return good, truncated

    @staticmethod
    def _always_present(consistent: list[list[str]]) -> list[int]:
        """在每个通过承诺的子集中都出现的 x（强嫌疑排除线索，非指控）。"""
        if not consistent:
            return []
        xs_per_subset = [
            {int(entry.split(":", 1)[0][2:]) for entry in subset}
            for subset in consistent
        ]
        intersection = set.intersection(*xs_per_subset)
        return sorted(intersection)
