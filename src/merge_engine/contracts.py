"""模块间的数据契约。

流向（见 README“模块关系”）：

    原始文件/JSON
        │  adapter.load_source / store.load_snapshot
        ▼
    SourceRow / TargetRow（普通 dict 行 + 稳定元数据）
        │  planner.plan_merge
        ▼
    MergePlan（纯数据：action 列表 + 操作前快照指纹）
        │  validator.validate_plan（资源/可序列化检查）
        ▼
    store.apply_plan（单个 IMMEDIATE 事务内提交）
        ▼
    RunResult（决策结果 + 动作集合 + 计数）

关键设计：MergePlan 是 *决策*，与 *提交* 分离。决策只依赖操作前目标快照，
因此同一 (source, pre-image, spec) 永远产生同一计划，且可以先 dry-run 审核。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# NULL 在键元组与行值中的统一表示
NULL = None

# 复合键：键列值的有序元组，NULL 即 Python None
Key = tuple[Any, ...]


def jsonable(value: Any) -> Any:
    """把含 bytes 的结构转成 JSON 安全形态（bytes -> {"__b64__": ...}）。

    动作的 before/after/key 都可能携带 bytes；API 响应、日志与可序列化
    校验统一经过这里，存储层的字节保真不受影响（写库走 MERGEVAL 编码）。
    """
    if isinstance(value, bytes):
        return {"__b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


class ActionType(str, Enum):
    UPDATE_MATCHED = "UPDATE_MATCHED"
    INSERT_UNMATCHED = "INSERT_UNMATCHED"
    DELETE_UNMATCHED = "DELETE_UNMATCHED"
    NOOP_MATCHED = "NOOP_MATCHED"          # 匹配但更新条件不成立
    NOOP_UNMATCHED = "NOOP_UNMATCHED"      # 未匹配但插入条件不成立（审计动作，不写库）


class NullEquality(str, Enum):
    """NULL 相等策略（仅影响“键匹配”阶段，不影响条件表达式）。

    SQL:       SQL 三值逻辑——任何键列为 NULL 即无法定位，源行直接拒绝
               (KEY_NULL_REJECTED)；目标键 NULL 不与任何源键匹配。
    DISTINCT:  NULL_IS_VALUE——NULL 与 NULL 视为相等（IS NOT DISTINCT FROM），
               非 NULL 按常规相等比较。
    """

    SQL = "SQL"
    DISTINCT = "DISTINCT"


@dataclass(frozen=True)
class SourceRow:
    rownum: int          # 从 1 开始的源内行号（稳定定位用）
    values: dict[str, Any]


@dataclass(frozen=True)
class TargetRow:
    rowid: int           # SQLite rowid，目标行的物理身份
    values: dict[str, Any]


@dataclass(frozen=True)
class Action:
    """一个已决定的动作。决策阶段产出，提交阶段消费。"""

    type: ActionType
    key: Key
    # 审计字段（全部 JSON 可序列化）
    source_rownum: int | None = None
    target_rowid: int | None = None
    before: dict[str, Any] | None = None     # UPDATE/DELETE 的操作前行
    after: dict[str, Any] | None = None      # UPDATE/INSERT 的操作后行
    reason: str = ""                          # 判定理由（含条件为何不成立）

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "key": [jsonable(v) for v in self.key],
            "source_rownum": self.source_rownum,
            "target_rowid": self.target_rowid,
            "before": jsonable(self.before),
            "after": jsonable(self.after),
            "reason": self.reason,
        }


@dataclass
class MergePlan:
    target_table: str
    key_columns: tuple[str, ...]
    payload_columns: tuple[str, ...]
    null_equality: NullEquality
    actions: list[Action] = field(default_factory=list)
    # 操作前快照指纹：让“决策基于操作前快照”可被外部核对
    snapshot_rowids: tuple[int, ...] = ()
    snapshot_fingerprint: str = ""
    # 校验后的动作计数（只含真正写库的三类）
    write_counts: dict[str, int] = field(default_factory=dict)

    def write_actions(self) -> list[Action]:
        return [
            a
            for a in self.actions
            if a.type
            in (
                ActionType.UPDATE_MATCHED,
                ActionType.INSERT_UNMATCHED,
                ActionType.DELETE_UNMATCHED,
            )
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_table": self.target_table,
            "key_columns": list(self.key_columns),
            "payload_columns": list(self.payload_columns),
            "null_equality": self.null_equality.value,
            "snapshot_rowids": list(self.snapshot_rowids),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "write_counts": self.write_counts,
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class RunResult:
    run_id: str
    dry_run: bool
    status: str                 # COMMITTED / REJECTED / FAILED
    target_table: str
    plan: MergePlan | None
    counts: dict[str, int]
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "status": self.status,
            "target_table": self.target_table,
            "counts": self.counts,
            "error": self.error,
            "plan": self.plan.to_dict() if self.plan is not None else None,
        }
