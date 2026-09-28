"""核心数据模型与失败类别。

三态：
- ALLOW  明确允许
- DENY   明确拒绝（显式拒绝优先）
- UNKNOWN 条件含未知值、无法确定；绝不默认为允许。

一个策略对单个请求的判定遵循：
1. 任何明确 DENY 命中 -> DENY（显式拒绝优先）
2. 否则任何明确 ALLOW 命中 -> ALLOW
3. 否则任何“可能命中”（因未知属性而无法排除的）规则 -> UNKNOWN
4. 否则 -> DENY（默认拒绝）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 三态判定
# ---------------------------------------------------------------------------
class Verdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"

    @property
    def is_conclusive(self) -> bool:
        return self is not Verdict.UNKNOWN


# 规则效果（策略文本里只允许两种确定效果；UNKNOWN 永远不能被策略显式写出）
class Effect(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


# 支持的条件算子。否定算子（Not*）必须在三态下保持保守：
# 条件未知时，Eq 的“可能为真”与 NotEq 的“可能为真”都为 True。
class ConditionOp(str, Enum):
    EQ = "Eq"
    NOT_EQ = "NotEq"
    IN = "In"
    NOT_IN = "NotIn"
    GREATER_THAN = "Gt"
    LESS_THAN = "Lt"
    GREATER_EQUAL = "Gte"
    LESS_EQUAL = "Lte"
    EXISTS = "Exists"
    NOT_EXISTS = "NotExists"
    CIDR_MATCH = "CidrMatch"
    NOT_CIDR_MATCH = "NotCidrMatch"
    GLOB_MATCH = "GlobMatch"
    NOT_GLOB_MATCH = "NotGlobMatch"


NUMERIC_OPS = {
    ConditionOp.GREATER_THAN,
    ConditionOp.LESS_THAN,
    ConditionOp.GREATER_EQUAL,
    ConditionOp.LESS_EQUAL,
}


# ---------------------------------------------------------------------------
# 失败类别（接口与日志中单列；不是普通字符串）
# 命名约定：SCOPE_*   空间/范围问题
#           SCHEMA_*  输入结构问题
#           CRYPTO_*  签名/信任问题
#           EVIDENCE_* 证据问题
# ---------------------------------------------------------------------------
class FailureKind(str, Enum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SCHEMA_VERSION_CONFLICT = "SCHEMA_VERSION_CONFLICT"
    SCOPE_NO_OVERLAP = "SCOPE_NO_OVERLAP"
    SCOPE_UNIVERSE_DEFINITION = "SCOPE_UNIVERSE_DEFINITION"
    SCOPE_SPACE_TOO_LARGE = "SCOPE_SPACE_TOO_LARGE"
    CRYPTO_MISSING_SIGNATURE = "CRYPTO_MISSING_SIGNATURE"
    CRYPTO_BAD_SIGNATURE = "CRYPTO_BAD_SIGNATURE"
    CRYPTO_UNREGISTERED_KEY = "CRYPTO_UNREGISTERED_KEY"
    EVIDENCE_TAMPERED = "EVIDENCE_TAMPERED"
    NOT_FOUND = "NOT_FOUND"


class DiffAnalyzerError(Exception):
    """所有可预期失败都以该异常（或子类）表达，并携带失败类别。"""

    kind: FailureKind = FailureKind.SCHEMA_INVALID

    def __init__(self, message: str,
                 details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_failure(self) -> "Failure":
        return Failure(kind=self.kind, message=self.message, details=self.details)


class SchemaError(DiffAnalyzerError):
    kind = FailureKind.SCHEMA_INVALID


class VersionConflictError(DiffAnalyzerError):
    kind = FailureKind.SCHEMA_VERSION_CONFLICT


class NoOverlapError(DiffAnalyzerError):
    kind = FailureKind.SCOPE_NO_OVERLAP


class UniverseDefinitionError(DiffAnalyzerError):
    kind = FailureKind.SCOPE_UNIVERSE_DEFINITION


class SpaceTooLargeError(DiffAnalyzerError):
    kind = FailureKind.SCOPE_SPACE_TOO_LARGE


class CryptoError(DiffAnalyzerError):
    kind = FailureKind.CRYPTO_BAD_SIGNATURE


class UnregisteredKeyError(CryptoError):
    kind = FailureKind.CRYPTO_UNREGISTERED_KEY


class MissingSignatureError(CryptoError):
    kind = FailureKind.CRYPTO_MISSING_SIGNATURE


class EvidenceError(DiffAnalyzerError):
    kind = FailureKind.EVIDENCE_TAMPERED


class NotFoundError(DiffAnalyzerError):
    kind = FailureKind.NOT_FOUND


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 规则 / 策略（解析后的不可变表示）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Condition:
    attribute: str
    op: ConditionOp
    value: Any = None  # Eq/In/数值/CIDR/glob 的右值；Exists 系列为 None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attribute": self.attribute,
            "op": self.op.value,
            "value": self.value,
        }


@dataclass(frozen=True)
class Rule:
    id: str
    effect: Effect
    # 资源前缀（目录前缀）。"" 表示桶根/全空间。
    resource_prefix: str
    # 操作集合。空集表示“不匹配任何操作”（而不是全部）。
    actions: frozenset[str]
    # 主体集合；frozenset() 表示不匹配任何具名主体；
    # 含 "*" 时同时匹配匿名。另有 principal_anonymous 显式位。
    principals: frozenset[str]
    anonymous: bool  # 是否适用于匿名（未鉴权）请求
    conditions: tuple[Condition, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "effect": self.effect.value,
            "resource_prefix": self.resource_prefix,
            "actions": sorted(self.actions),
            "principals": sorted(self.principals),
            "anonymous": self.anonymous,
            "conditions": [c.to_dict() for c in self.conditions],
        }


@dataclass(frozen=True)
class Policy:
    version: str
    rules: tuple[Rule, ...]
    # 输入原文的规范化哈希（SHA-256 hex），用于状态隔离与审计
    source_hash: str
    submitted_by: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rules": [r.to_dict() for r in self.rules],
            "source_hash": self.source_hash,
            "submitted_by": self.submitted_by,
        }


# ---------------------------------------------------------------------------
# 待判定请求（受限请求空间中的一个点）
# ---------------------------------------------------------------------------
# 属性“未知”的哨兵：与 Python None（表示显式为空）区分开。
UNKNOWN = object()


@dataclass(frozen=True)
class Request:
    principal: Optional[str]       # None 表示匿名
    action: str
    resource: str
    attributes: tuple[tuple[str, Any], ...]

    @classmethod
    def make(
        cls,
        principal: Optional[str],
        action: str,
        resource: str,
        attributes: Optional[dict[str, Any]] = None,
    ) -> "Request":
        return cls(
            principal=principal,
            action=action,
            resource=resource,
            attributes=tuple(sorted((attributes or {}).items())),
        )

    def attr(self, name: str) -> Any:
        for k, v in self.attributes:
            if k == name:
                return v
        return UNKNOWN


# ---------------------------------------------------------------------------
# 判定轨迹（可解释性：关键步骤）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TraceStep:
    step: int
    rule_id: str
    effect: Optional[str]
    prefix_match: Optional[bool]
    action_match: Optional[bool]
    principal_match: Optional[bool]
    condition_results: tuple[tuple[str, str, str], ...]  # (attr, op, TRUE/FALSE/UNKNOWN)
    rule_outcome: str  # MATCH / NOT_MATCH / POSSIBLE_MATCH

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "rule_id": self.rule_id,
            "effect": self.effect,
            "prefix_match": self.prefix_match,
            "action_match": self.action_match,
            "principal_match": self.principal_match,
            "condition_results": [
                {"attribute": a, "op": op, "result": r}
                for a, op, r in self.condition_results
            ],
            "rule_outcome": self.rule_outcome,
        }


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    decided_by: Optional[str]            # 决定性规则 id；默认拒绝时为 None
    default_deny: bool                   # 是否因默认拒绝而 DENY
    trace: tuple[TraceStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "decided_by": self.decided_by,
            "default_deny": self.default_deny,
            "trace": [t.to_dict() for t in self.trace],
        }
