"""领域模型：表 schema、不可变快照、行级三方判定与合并报告。

记录身份由 *主键值* 标识（而非文件名），字段值参与比较。
"""
from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# 支持的列类型 -> PyArrow 构建时使用的逻辑名
SUPPORTED_TYPES = frozenset({"int64", "float64", "string", "bool"})


class RowDecision(str, enum.Enum):
    """单行的自动判定结果。只有 *_CONFLICT 三类需要人工解决。"""

    UNCHANGED = "UNCHANGED"                       # 两侧都未改
    FAST_FORWARD = "FAST_FORWARD"                 # 仅一侧改动，直接采用
    FIELD_MERGE = "FIELD_MERGE"                   # 两侧改不同字段，字段级合并
    SAME_FIELD_CONFLICT = "SAME_FIELD_CONFLICT"   # 两侧改了同一字段且值不同
    DELETE_MODIFY_CONFLICT = "DELETE_MODIFY_CONFLICT"  # 一侧删、另一侧改
    ADD_ADD_CONFLICT = "ADD_ADD_CONFLICT"         # 两侧新增同主键、内容不同

    @property
    def is_conflict(self) -> bool:
        return self in (
            RowDecision.SAME_FIELD_CONFLICT,
            RowDecision.DELETE_MODIFY_CONFLICT,
            RowDecision.ADD_ADD_CONFLICT,
        )


class ResolutionAction(str, enum.Enum):
    """冲突解决动作（绑定具体三方快照，见 merge_resolutions 表）。"""

    USE_DEV = "USE_DEV"     # 采用开发分支行；对删除/修改冲突表示“保留修改”
    USE_MAIN = "USE_MAIN"   # 采用主分支行；对删除/修改冲突表示“保留修改”
    KEEP_DELETED = "KEEP_DELETED"  # 接受删除（仅删除/修改冲突合法）
    FIELD_PICK = "FIELD_PICK"      # 逐字段选择（字段名 -> DEV/MAIN），非新增/新增冲突可用


CONFLICT_ACTIONS = {
    RowDecision.SAME_FIELD_CONFLICT: {ResolutionAction.USE_DEV, ResolutionAction.USE_MAIN,
                                      ResolutionAction.FIELD_PICK},
    RowDecision.DELETE_MODIFY_CONFLICT: {ResolutionAction.USE_DEV, ResolutionAction.USE_MAIN,
                                         ResolutionAction.KEEP_DELETED},
    RowDecision.ADD_ADD_CONFLICT: {ResolutionAction.USE_DEV, ResolutionAction.USE_MAIN},
}


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # SUPPORTED_TYPES 之一


@dataclass(frozen=True)
class TableSchema:
    table: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]

    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def type_map(self) -> dict[str, str]:
        return {c.name: c.type for c in self.columns}

    def validate(self) -> None:
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate column names in schema for {self.table}")
        for c in self.columns:
            if c.type not in SUPPORTED_TYPES:
                raise ValueError(f"unsupported column type {c.type!r} for {self.table}.{c.name}")
        if not self.primary_key:
            raise ValueError(f"table {self.table} must declare a primary key")
        for pk in self.primary_key:
            if pk not in names:
                raise ValueError(f"primary key column {pk!r} missing from schema of {self.table}")

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "columns": [{"name": c.name, "type": c.type} for c in self.columns],
            "primary_key": list(self.primary_key),
        }

    @staticmethod
    def from_dict(data: dict) -> "TableSchema":
        schema = TableSchema(
            table=data["table"],
            columns=tuple(Column(c["name"], c["type"]) for c in data["columns"]),
            primary_key=tuple(data["primary_key"]),
        )
        schema.validate()
        return schema


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    table: str
    schema: TableSchema
    parquet_path: str
    row_count: int
    content_hash: str  # 全部数据文件的 sha256，用于幂等去重
    reused: bool = False  # 本次导入是否命中既有内容（未新写文件）

    def to_metadata_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "table_name": self.table,
            "schema_json": json.dumps(self.schema.to_dict(), sort_keys=True),
            "parquet_path": self.parquet_path,
            "row_count": self.row_count,
            "content_hash": self.content_hash,
        }


def key_tuple(row: dict[str, Any], primary_key: tuple[str, ...]) -> tuple:
    return tuple(row[k] for k in primary_key)


def key_string(key: tuple) -> str:
    """主键的稳定字符串形式（用于冲突索引与日志）。"""
    return json.dumps(list(key), separators=(",", ":"), ensure_ascii=False, sort_keys=False)


@dataclass
class RowDecisionDetail:
    key: tuple
    decision: RowDecision
    basis: str                       # 判定依据的人类可读说明
    changed_fields_dev: tuple[str, ...] = ()
    changed_fields_main: tuple[str, ...] = ()
    dev_row: dict | None = None
    main_row: dict | None = None

    def to_dict(self) -> dict:
        return {
            "key": list(self.key),
            "decision": self.decision.value,
            "is_conflict": self.decision.is_conflict,
            "basis": self.basis,
            "changed_fields_dev": list(self.changed_fields_dev),
            "changed_fields_main": list(self.changed_fields_main),
            "dev_row": self.dev_row,
            "main_row": self.main_row,
        }


@dataclass
class MergeReport:
    base_snapshot_id: str
    dev_snapshot_id: str
    main_snapshot_id: str
    merged_rows: list[dict]          # 自动合并的行（冲突行不在其中）
    decisions: list[RowDecisionDetail]
    steps: list[str] = field(default_factory=list)   # 计算步骤轨迹

    @property
    def conflicts(self) -> list[RowDecisionDetail]:
        return [d for d in self.decisions if d.decision.is_conflict]

    @property
    def conflict_keys(self) -> list[tuple]:
        return [d.key for d in self.conflicts]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.decisions:
            out[d.decision.value] = out.get(d.decision.value, 0) + 1
        return out

    def to_plan_dict(self) -> dict:
        return {
            "base_snapshot_id": self.base_snapshot_id,
            "dev_snapshot_id": self.dev_snapshot_id,
            "main_snapshot_id": self.main_snapshot_id,
            "merged_row_count": len(self.merged_rows),
            "counts": self.counts(),
            "steps": self.steps,
            "conflicts": [d.to_dict() for d in self.conflicts],
            "automatic_rows": [d.to_dict() for d in self.decisions if not d.decision.is_conflict],
        }


def stable_hash(payload: Any) -> str:
    """对 JSON 可序列化结构计算稳定 sha256（提交/计划 ID 用）。"""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
