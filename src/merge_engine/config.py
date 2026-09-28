"""MERGE 规格（配置）：表、键、NULL 策略、三类条件与资源上限。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .conditions import Condition, ConditionScope, parse_condition
from .contracts import NullEquality
from .errors import ConfigError

UPDATE_SCOPE = ConditionScope("update", frozenset({"source", "target"}))
INSERT_SCOPE = ConditionScope("insert", frozenset({"source"}))
DELETE_SCOPE = ConditionScope("delete", frozenset({"target"}))

# 资源上限默认值（也在适配器早期拦截，构成 RESOURCE_EXHAUSTED 而非 OOM）
DEFAULT_MAX_SOURCE_ROWS = 1_000_000
DEFAULT_MAX_ACTIONS = 2_000_000
DEFAULT_MAX_PLAN_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class MergeSpec:
    target_table: str
    key_columns: tuple[str, ...]
    null_equality: NullEquality
    # 条件为 None 表示“无条件”：update/insert 恒成立；删除集合恒为空
    update_when: Condition | None
    insert_when: Condition | None
    delete_unmatched: bool
    delete_when: Condition | None
    max_source_rows: int
    max_actions: int
    max_plan_bytes: int

    def all_columns(self, source_columns: tuple[str, ...]) -> frozenset[str]:
        return frozenset(self.key_columns) | frozenset(source_columns)


def _parse_optional_condition(
    raw: Any,
    scope: ConditionScope,
    columns: frozenset[str],
) -> Condition | None:
    if raw is None:
        return None
    return parse_condition(raw, scope, columns)


def build_spec(
    raw: dict[str, Any],
    source_columns: tuple[str, ...],
    target_columns: tuple[str, ...] = (),
) -> MergeSpec:
    """把 API/调用方的原始 dict 配置解析成 MergeSpec。

    source_columns 在此刻已知（适配器先读源）；target_columns 为操作前目标表
    的列（dry-run 且表不存在时为空）。条件引用的列当场校验。
    """
    table = raw.get("target_table")
    if not isinstance(table, str) or not table.strip():
        raise ConfigError("target_table must be a non-empty string")
    if not table.replace("_", "").isalnum():
        raise ConfigError(
            "target_table may contain only letters, digits and underscore",
            details={"target_table": table},
        )

    keys = raw.get("key_columns")
    if not isinstance(keys, list) or not keys:
        raise ConfigError("key_columns must be a non-empty list")
    if any(not isinstance(k, str) for k in keys):
        raise ConfigError("key_columns must contain only strings")
    if len(set(keys)) != len(keys):
        raise ConfigError("key_columns must not contain duplicates", details={"key_columns": keys})

    try:
        null_eq = NullEquality(str(raw.get("null_equality", "SQL")).upper())
    except ValueError:
        raise ConfigError(
            "null_equality must be SQL or DISTINCT",
            details={"got": raw.get("null_equality")},
        )

    columns = frozenset(keys) | frozenset(source_columns) | frozenset(target_columns)

    update_when = _parse_optional_condition(raw.get("update_when"), UPDATE_SCOPE, columns)
    insert_when = _parse_optional_condition(raw.get("insert_when"), INSERT_SCOPE, columns)
    delete_when = _parse_optional_condition(raw.get("delete_when"), DELETE_SCOPE, columns)

    delete_unmatched = bool(raw.get("delete_unmatched", False))
    if delete_unmatched and delete_when is None:
        # 开了删除策略却没给条件：要么明确给恒真条件，要么拒绝歧义
        raise ConfigError("delete_unmatched=true requires an explicit delete_when condition")

    max_source_rows = int(raw.get("max_source_rows", DEFAULT_MAX_SOURCE_ROWS))
    max_actions = int(raw.get("max_actions", DEFAULT_MAX_ACTIONS))
    max_plan_bytes = int(raw.get("max_plan_bytes", DEFAULT_MAX_PLAN_BYTES))
    for name, value in (
        ("max_source_rows", max_source_rows),
        ("max_actions", max_actions),
        ("max_plan_bytes", max_plan_bytes),
    ):
        if value <= 0:
            raise ConfigError(f"{name} must be positive", details={"value": value})

    return MergeSpec(
        target_table=table,
        key_columns=tuple(keys),
        null_equality=null_eq,
        update_when=update_when,
        insert_when=insert_when,
        delete_unmatched=delete_unmatched,
        delete_when=delete_when,
        max_source_rows=max_source_rows,
        max_actions=max_actions,
        max_plan_bytes=max_plan_bytes,
    )
