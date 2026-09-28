"""规则与策略解析：严格校验，拒绝一切未知字段/未知算子。

设计原则：
- 解析失败必须显式报错（SCHEMA_INVALID），不做“猜测性容错”。
- 解析结果是不可变的领域对象（models.Rule / Policy），内核不接触原始 dict。
- 这里不做任何安全判定，只做语法/结构/类型校验。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .models import (
    Condition,
    ConditionOp,
    Effect,
    NUMERIC_OPS,
    Policy,
    Rule,
    SchemaError,
)

_ALLOWED_TOP = {"version", "rules"}
_ALLOWED_RULE = {
    "id",
    "effect",
    "resource_prefix",
    "actions",
    "principals",
    "anonymous",
    "conditions",
}
_ALLOWED_COND = {"attribute", "op", "value"}

# 条件属性的受控命名空间：未知属性名本身合法（请求可能携带），
# 但保留前缀与内置键冲突要拒绝，避免“看起来生效其实静默忽略”。
_RESERVED_ATTRS = {"principal", "action", "resource"}


def canonical_json_bytes(obj: Any) -> bytes:
    """规范 JSON（排序键、无多余空白、UTF-8）。签名/哈希共用。"""

    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def source_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(obj)).hexdigest()


# ---------------------------------------------------------------------------
# 标量校验
# ---------------------------------------------------------------------------
def _require_str(value: Any, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{what} 必须是字符串", {"got_type": type(value).__name__})
    if not allow_empty and value == "":
        raise SchemaError(f"{what} 不能为空字符串")
    return value


def _require_list_of_str(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SchemaError(f"{what} 必须是字符串列表", {"got_type": type(value).__name__})
    return value


def _reject_extra(obj: dict[str, Any], allowed: set[str], what: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise SchemaError(
            f"{what} 含未知字段: {sorted(extra)}", {"unknown_fields": sorted(extra)}
        )


# ---------------------------------------------------------------------------
# 条件校验
# ---------------------------------------------------------------------------
def _validate_condition(raw: Any, index: int) -> Condition:
    what = f"rules[{index}].conditions"
    if not isinstance(raw, dict):
        raise SchemaError(f"{what}[?] 必须是对象")
    _reject_extra(raw, _ALLOWED_COND, what)

    attribute = _require_str(raw.get("attribute"), f"{what}.attribute")
    if attribute in _RESERVED_ATTRS:
        raise SchemaError(
            f"{what}.attribute 不得使用保留名 {attribute!r}",
            {"reserved": sorted(_RESERVED_ATTRS)},
        )
    if attribute.startswith("principal.") or attribute.startswith("__"):
        raise SchemaError(
            f"{what}.attribute 不得使用保留前缀: {attribute!r}",
            {"attribute": attribute},
        )

    op_raw = _require_str(raw.get("op"), f"{what}.op")
    try:
        op = ConditionOp(op_raw)
    except ValueError:
        raise SchemaError(
            f"{what}.op 未知算子: {op_raw!r}",
            {"allowed": [o.value for o in ConditionOp]},
        ) from None

    has_value = "value" in raw
    value = raw.get("value")

    if op in (ConditionOp.EXISTS, ConditionOp.NOT_EXISTS):
        if has_value:
            raise SchemaError(f"{op.value} 条件不接受 value", {"attribute": attribute})
        return Condition(attribute=attribute, op=op)

    if not has_value:
        raise SchemaError(f"{op.value} 条件必须提供 value", {"attribute": attribute})

    if op in (ConditionOp.EQ, ConditionOp.NOT_EQ):
        # 右值允许字符串/数字/布尔；列表必须用 In
        if isinstance(value, list) or isinstance(value, dict) or value is None:
            raise SchemaError(
                f"{op.value} 的 value 必须是标量（字符串/数字/布尔）",
                {"attribute": attribute, "got_type": type(value).__name__},
            )
    elif op in (ConditionOp.IN, ConditionOp.NOT_IN):
        items = _require_list_of_str(value, f"{op.value} 的 value")
        if not items:
            raise SchemaError(f"{op.value} 的 value 列表不能为空", {"attribute": attribute})
        if len(set(items)) != len(items):
            raise SchemaError(f"{op.value} 的 value 含重复元素", {"attribute": attribute})
    elif op in NUMERIC_OPS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(
                f"{op.value} 的 value 必须是数字",
                {"attribute": attribute, "got_type": type(value).__name__},
            )
    elif op in (ConditionOp.CIDR_MATCH, ConditionOp.NOT_CIDR_MATCH):
        cidr = _require_str(value, f"{op.value} 的 value")
        _validate_cidr(cidr, attribute)
    elif op in (ConditionOp.GLOB_MATCH, ConditionOp.NOT_GLOB_MATCH):
        _require_str(value, f"{op.value} 的 value", allow_empty=True)
        _validate_glob(value, attribute)

    return Condition(attribute=attribute, op=op, value=value)


def _validate_cidr(cidr: str, attribute: str) -> None:
    # 用 ipaddress 做真实语法校验，拒绝 "not-a-cidr"
    import ipaddress

    try:
        ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise SchemaError(
            f"CidrMatch 的 value 不是合法 CIDR: {cidr!r}",
            {"attribute": attribute, "detail": str(exc)},
        ) from None


def _validate_glob(pattern: str, attribute: str) -> None:
    # 只允许 * 与 ? 元字符；拒绝字符类等可能造成方言歧义的写法
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[" :
            raise SchemaError(
                "GlobMatch 不支持字符类 [..]，仅支持 * 与 ?",
                {"attribute": attribute, "pattern": pattern},
            )
        i += 1


# ---------------------------------------------------------------------------
# 规则 / 策略校验
# ---------------------------------------------------------------------------
def _validate_rule(raw: Any, index: int) -> Rule:
    what = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise SchemaError(f"{what} 必须是对象")
    _reject_extra(raw, _ALLOWED_RULE, what)

    rule_id = _require_str(raw.get("id"), f"{what}.id")

    effect_raw = _require_str(raw.get("effect"), f"{what}.effect")
    try:
        effect = Effect(effect_raw)
    except ValueError:
        raise SchemaError(
            f"{what}.effect 必须是 ALLOW 或 DENY（禁止显式 UNKNOWN）",
            {"got": effect_raw},
        ) from None

    # resource_prefix: 允许 ""（桶根）；必须是非 None 字符串
    prefix = raw.get("resource_prefix")
    if not isinstance(prefix, str):
        raise SchemaError(
            f"{what}.resource_prefix 必须是字符串（空串表示桶根）",
            {"got_type": type(prefix).__name__},
        )
    # 前缀按目录边界理解：非空前缀若不以 / 结尾，语义是“该前缀下的所有键”，
    # 这里统一规范化（追加 /）以消除 "logs" 误匹配 "logs-secret" 的边界错误。
    if prefix != "" and not prefix.endswith("/"):
        prefix = prefix + "/"

    actions = _require_list_of_str(raw.get("actions"), f"{what}.actions")
    if not actions:
        raise SchemaError(f"{what}.actions 不能为空（空集=不匹配任何操作，需显式表达）")
    if len(set(actions)) != len(actions):
        raise SchemaError(f"{what}.actions 含重复操作", {"rule_id": rule_id})

    principals_raw = raw.get("principals", [])
    principals = _require_list_of_str(principals_raw, f"{what}.principals")
    if len(set(principals)) != len(principals):
        raise SchemaError(f"{what}.principals 含重复主体", {"rule_id": rule_id})

    anonymous = raw.get("anonymous", False)
    if not isinstance(anonymous, bool):
        raise SchemaError(f"{what}.anonymous 必须是布尔值")

    # "*" 与 anonymous 的一致性：写了 "*" 即覆盖匿名，anonymous 必须为 true，
    # 避免两处表达不一致造成静默歧义。
    if "*" in principals and not anonymous:
        raise SchemaError(
            f"{what}: principals 含 '*' 时 anonymous 必须为 true",
            {"rule_id": rule_id},
        )
    if anonymous and not ("*" in principals):
        # anonymous=true 表示规则适用匿名；此时具名主体可为空，允许
        pass

    conds_raw = raw.get("conditions", [])
    if not isinstance(conds_raw, list):
        raise SchemaError(f"{what}.conditions 必须是列表")
    conditions = tuple(_validate_condition(c, i) for i, c in enumerate(conds_raw))

    return Rule(
        id=rule_id,
        effect=effect,
        resource_prefix=prefix,
        actions=frozenset(actions),
        principals=frozenset(principals),
        anonymous=anonymous,
        conditions=conditions,
    )


def parse_policy(raw: Any, *, submitted_by: str | None = None) -> Policy:
    """解析并严格校验一份策略文档。"""

    if not isinstance(raw, dict):
        raise SchemaError("策略文档必须是 JSON 对象")
    _reject_extra(raw, _ALLOWED_TOP, "策略根对象")

    version = _require_str(raw.get("version"), "version")
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list):
        raise SchemaError("rules 必须是列表")

    rules = tuple(_validate_rule(r, i) for i, r in enumerate(rules_raw))

    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        raise SchemaError("规则 id 必须唯一", {"duplicates": dupes})

    return Policy(
        version=version,
        rules=rules,
        source_hash=source_hash(raw),
        submitted_by=submitted_by,
    )


def parse_actions(raw: Any) -> frozenset[str]:
    """解析分析范围（操作集合）。"""
    items = _require_list_of_str(raw, "actions")
    if not items:
        raise SchemaError("分析范围 actions 不能为空")
    if len(set(items)) != len(items):
        raise SchemaError("分析范围 actions 含重复操作")
    return frozenset(items)


def parse_prefixes(raw: Any) -> tuple[str, ...]:
    """解析分析范围（资源前缀），同样规范化目录边界。"""
    items = _require_list_of_str(raw, "resource_prefixes")
    if not items:
        raise SchemaError("分析范围 resource_prefixes 不能为空")
    norm = []
    for p in items:
        norm.append("" if p == "" else (p if p.endswith("/") else p + "/"))
    if len(set(norm)) != len(norm):
        raise SchemaError("分析范围 resource_prefixes 规范化后重复", {"prefixes": norm})
    return tuple(norm)
