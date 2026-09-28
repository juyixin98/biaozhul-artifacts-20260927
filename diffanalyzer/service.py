"""应用服务层：把解析、验签、状态、内核/穷举、审计串成用例。

本层不处理 HTTP 细节（由 api 层负责），便于 CLI 与测试直接复用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .audit import Auditor
from .config import Config, load_config
from .crypto_verify import KeyRegistry
from .diffengine import DiffResult, compute_diff
from .evidence import parse_evidence_bundle
from .models import (
    DiffAnalyzerError,
    FailureKind,
    Policy,
    SchemaError,
)
from .parser import parse_actions, parse_policy, parse_prefixes
from .store import Store


class PolicyService:
    def __init__(self, config: Config, store: Store, auditor: Auditor,
                 registry: KeyRegistry):
        self.cfg = config
        self.store = store
        self.auditor = auditor
        self.registry = registry

    # ------------------------------------------------------------------
    # 策略提交
    # ------------------------------------------------------------------
    def submit_policy(self, envelope: dict[str, Any], *,
                      request_id: str, actor: str) -> Policy:
        """完整管线：结构检查 -> 签名核验 -> 严格解析 -> 不可变落库。"""
        try:
            claim = envelope.get("signed_at_claim")
            submitted_by = envelope.get("submitted_by")
            if not isinstance(submitted_by, str):
                raise SchemaError("信封 submitted_by 必须是字符串")
            if not isinstance(claim, dict):
                raise SchemaError("信封缺少 signed_at_claim")
            version = claim.get("version")
            policy_doc = claim.get("policy")
            signature = envelope.get("signature")
            if not isinstance(version, str) or not isinstance(policy_doc, dict):
                raise SchemaError("signed_at_claim 必须含 version 与 policy 对象")
            if policy_doc.get("version") != version:
                raise SchemaError(
                    "声明版本与策略文档内 version 不一致",
                    {"claim_version": version,
                     "doc_version": policy_doc.get("version")},
                )

            # 1) 密码学核验（未知公钥/坏签名在此中止）
            self.auditor.event(
                request_id=request_id, actor=actor, component="crypto_verify",
                stage="verify-policy-signature", version=version,
                summary=f"核验提交者 {submitted_by} 的 Ed25519 签名",
            )
            self.registry.verify(submitted_by, claim, signature)

            # 2) 严格解析（边界错误在此显式失败而非静默忽略）
            self.auditor.event(
                request_id=request_id, actor=actor, component="parser",
                stage="parse-policy", version=version,
            )
            policy = parse_policy(policy_doc, submitted_by=submitted_by)

            # 3) 不可变快照
            self.store.save_policy(
                version=policy.version,
                source_hash=policy.source_hash,
                submitted_by=submitted_by,
                envelope=envelope,
                parsed=policy.to_dict(),
            )
            self.auditor.event(
                request_id=request_id, actor=actor, component="store",
                stage="persist-policy", status="OK", version=policy.version,
                summary=f"策略 {policy.version} 已存为不可变快照"
                        f"（{len(policy.rules)} 条规则，sha256={policy.source_hash[:12]}…）",
            )
            return policy
        except DiffAnalyzerError as exc:
            self.auditor.failure(
                request_id=request_id, actor=actor,
                component="crypto_verify" if exc.kind.name.startswith("CRYPTO")
                else "parser",
                stage="submit-policy", kind=exc.kind,
                message=exc.message, detail=exc.details,
            )
            raise
        except Exception as exc:  # 防御：意外错误归类并审计
            self.auditor.failure(
                request_id=request_id, actor=actor, component="parser",
                stage="submit-policy", kind=FailureKind.SCHEMA_INVALID,
                message=f"策略提交失败: {exc}",
            )
            raise

    def load_policy(self, version: str) -> Optional[Policy]:
        parsed = self.store.get_policy_parsed(version)
        if parsed is None:
            return None
        return _policy_from_parsed(parsed)

    # ------------------------------------------------------------------
    # 证据提交
    # ------------------------------------------------------------------
    def submit_evidence(self, envelope: dict[str, Any], *,
                        request_id: str, actor: str) -> dict[str, Any]:
        # 先解析+重算哈希链（可能 EVIDENCE_TAMPERED）
        bundle = parse_evidence_bundle(envelope)
        # 再验签（CRYPTO_*）
        self.registry.verify(bundle.submitted_by, bundle.signed_claim(),
                             bundle.signature)
        if self.store.get_policy_parsed(bundle.policy_version) is None:
            raise SchemaError(
                "证据引用了系统中不存在的策略版本",
                {"policy_version": bundle.policy_version},
            )
        self.store.save_evidence_bundle(
            bundle_id=bundle.bundle_id,
            submitted_by=bundle.submitted_by,
            policy_version=bundle.policy_version,
            scope={
                "resource_prefixes": list(bundle.scope_prefixes),
                "actions": list(bundle.scope_actions),
            },
            record_count=len(bundle.records),
            envelope=envelope,
        )
        self.auditor.event(
            request_id=request_id, actor=actor, component="evidence",
            stage="persist-evidence",
            version=bundle.policy_version,
            summary=(
                f"证据束 {bundle.bundle_id} 验签与哈希链核验通过，"
                f"{len(bundle.records)} 条记录已存"
            ),
            detail={"bundle_id": bundle.bundle_id},
        )
        return {
            "bundle_id": bundle.bundle_id,
            "policy_version": bundle.policy_version,
            "record_count": len(bundle.records),
        }

    # ------------------------------------------------------------------
    # 差分分析
    # ------------------------------------------------------------------
    def run_diff(
        self,
        *,
        old_version: str,
        new_version: str,
        scope: dict[str, Any],
        request_id: str,
        actor: str,
        evidence_bundle_id: Optional[str] = None,
    ) -> DiffResult:
        # 纯策略差分要求两个不同版本；同版本仅在“证据对账”场景下允许
        # （用当前策略重放真实观测），这是两种不同用途。
        if old_version == new_version and not evidence_bundle_id:
            raise SchemaError("策略差分要求两个不同的版本",
                              {"version": old_version})

        old_policy = self.load_policy(old_version)
        new_policy = self.load_policy(new_version)
        missing = [
            v for v, p in ((old_version, old_policy), (new_version, new_policy))
            if p is None
        ]
        if missing:
            from .models import NotFoundError
            raise NotFoundError(
                "策略版本不存在，无法差分",
                {"missing_versions": missing},
            )

        scope_prefixes = parse_prefixes(scope.get("resource_prefixes"))
        scope_actions = parse_actions(scope.get("actions"))

        bundle = None
        if evidence_bundle_id:
            env = self.store.get_evidence_envelope(evidence_bundle_id)
            if env is None:
                from .models import NotFoundError
                raise NotFoundError(
                    "证据束不存在", {"bundle_id": evidence_bundle_id}
                )
            bundle = parse_evidence_bundle(env)
            self.registry.verify(bundle.submitted_by, bundle.signed_claim(),
                                 bundle.signature)

        self.auditor.event(
            request_id=request_id, actor=actor, component="diffengine",
            stage="start",
            version=new_version,
            summary=f"开始差分 {old_version} -> {new_version}",
            detail={"scope_prefixes": list(scope_prefixes),
                    "scope_actions": sorted(scope_actions),
                    "evidence_bundle_id": evidence_bundle_id},
        )

        result = compute_diff(
            old_policy=old_policy,
            new_policy=new_policy,
            scope_prefixes=scope_prefixes,
            scope_actions=scope_actions,
            resource_alphabet=self.cfg.resource_alphabet,
            configured_principals=self.cfg.principals,
            include_anonymous=self.cfg.include_anonymous,
            max_space_size=self.cfg.max_space_size,
            witness_limit=self.cfg.witness_limit_per_bucket,
            auditor=self.auditor,
            request_id=request_id,
            actor=actor,
            evidence_bundle=bundle,
        )
        self.store.save_diff(
            diff_id=result.diff_id,
            request_id=request_id,
            actor=actor,
            old_version=old_version,
            new_version=new_version,
            scope=result.scope,
            space_size=result.space["size"],
            enumeration_count=result.space["enumerated"],
            summary=result.summary,
            witnesses=result.witnesses,
            evidence=result.evidence_report,
        )
        self.auditor.event(
            request_id=request_id, actor=actor, component="store",
            stage="persist-diff", status="OK", diff_id=result.diff_id,
            summary=f"差分结果 {result.diff_id} 已持久化",
        )
        return result


def _policy_from_parsed(parsed: dict[str, Any]) -> Policy:
    """从存储的不可变 JSON 重建领域对象（不重新信任原始输入结构）。"""
    from .models import Condition, ConditionOp, Effect, Rule
    rules = []
    for r in parsed["rules"]:
        conds = tuple(
            Condition(
                attribute=c["attribute"],
                op=ConditionOp(c["op"]),
                value=c["value"] if "value" in c else None,
            )
            for c in r["conditions"]
        )
        rules.append(Rule(
            id=r["id"],
            effect=Effect(r["effect"]),
            resource_prefix=r["resource_prefix"],
            actions=frozenset(r["actions"]),
            principals=frozenset(r["principals"]),
            anonymous=r["anonymous"],
            conditions=conds,
        ))
    return Policy(
        version=parsed["version"],
        rules=tuple(rules),
        source_hash=parsed["source_hash"],
        submitted_by=parsed.get("submitted_by"),
    )


def build_default_service(*, db_path: Optional[str] = None,
                          config: Optional[Config] = None) -> tuple[
        PolicyService, Store, Auditor, Config]:
    cfg = config or load_config()
    store = Store(db_path or cfg.abs_path(cfg.sqlite_path))
    auditor = Auditor(store)
    pub_path: Path = cfg.abs_path(cfg.trusted_key_path)
    if not pub_path.exists():
        raise FileNotFoundError(
            f"受信任公钥不存在: {pub_path}；请先运行 fixtures/gen_keys.py"
        )
    registry = KeyRegistry.from_pem_dir({"submitter": pub_path})
    return PolicyService(cfg, store, auditor, registry), store, auditor, cfg
