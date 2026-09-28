"""领域模型（纯数据，不依赖 PyArrow / SQLite）。

记录身份（record identity）= 主键列上的值。三方比较以主键识别同一条记录，
而不是按行的位置或文件名比较——这是与"只比文件名"式合并的根本区别。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------- 枚举

class SideName(str, Enum):
    BASE = "base"
    OURS = "ours"
    THEIRS = "theirs"


class EntryClass(str, Enum):
    """逐条记录的三方分类（判定依据）。"""

    UNCHANGED = "unchanged"                       # 两边都未相对祖先变化
    OURS_MODIFIED = "ours_modified"               # 仅开发分支修改 -> 自动
    THEIRS_MODIFIED = "theirs_modified"           # 仅主分支修改 -> 自动
    OURS_DELETED = "ours_deleted"                 # 仅开发分支删除 -> 自动（删除）
    THEIRS_DELETED = "theirs_deleted"             # 仅主分支删除 -> 自动（删除）
    OURS_ADDED = "ours_added"                     # 仅开发分支新增 -> 自动
    THEIRS_ADDED = "theirs_added"                 # 仅主分支新增 -> 自动
    FIELD_MERGE = "field_merge"                   # 两边改不同字段 -> 自动（字段级合并）
    DELETE_MODIFY_CONFLICT = "delete_modify_conflict"  # 删除对修改（任一侧）
    FIELD_VALUE_CONFLICT = "field_value_conflict"     # 同一字段被两边改成不同值
    ADD_ADD_CONFLICT = "add_add_conflict"           # 同键独立新增且内容不同
    ADD_ADD_IDENTICAL = "add_add_identical"         # 同键独立新增且内容一致
    BOTH_DELETED = "both_deleted"                 # 两边都删除同一记录 -> 自动（删除）


class FieldOrigin(str, Enum):
    """合并后单个字段值的来源（字段级血缘）。"""

    BASE = "base"
    OURS = "ours"
    THEIRS = "theirs"
    AGREED = "agreed"       # 两边独立改成同一个值
    CONFLICT = "conflict"   # 待解决，暂存 base 值
    RESOLUTION = "resolution"


class ResolutionKind(str, Enum):
    OURS = "ours"
    THEIRS = "theirs"
    VALUE = "value"          # 显式给行（字段级取值冲突时可只给部分字段）
    DELETE = "delete"
    KEEP = "keep"            # 撤销删除（删除/修改冲突时保留被删行的另一侧版本）


class MergeStatus(str, Enum):
    OPEN = "open"
    COMMITTED = "committed"
    ABANDONED = "abandoned"


# ---------------------------------------------------------------- 表/Schema

@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str            # int64 | float64 | string | bool | date32 | timestamp_us
    nullable: bool = True


@dataclass
class TableSpec:
    name: str
    primary_key: list[str]
    fields: list[FieldSpec]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "primary_key": list(self.primary_key),
            "fields": [asdict(f) for f in self.fields],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TableSpec":
        return cls(
            name=d["name"],
            primary_key=list(d["primary_key"]),
            fields=[FieldSpec(**f) for f in d["fields"]],
        )


# ---------------------------------------------------------------- 快照/分支/提交

@dataclass
class Snapshot:
    """不可变表快照。content_hash 由规范化数据与 schema 决定（内容寻址）。"""

    snapshot_id: str
    table: str
    schema_version: int
    content_hash: str
    row_count: int
    parent_snapshot_id: str | None
    created_by_run_id: str | None
    created_at: str


@dataclass
class Branch:
    name: str
    table: str
    head_snapshot_id: str


# ---------------------------------------------------------------- 合并计划

@dataclass
class FieldDecision:
    """单字段判定。"""

    origin: str                 # FieldOrigin 的值
    value: Any
    ours_value: Any
    theirs_value: Any
    base_value: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MergeEntry:
    """一条记录键上的三方判定结果。"""

    key: dict[str, Any]
    classification: str         # EntryClass 的值
    conflict: bool
    reason: str                 # 人类可读的判定依据
    merged: dict[str, Any] | None       # 自动合并后的行；删除或未决冲突时为 None
    deleted: bool                        # 自动判定的结果是否为删除
    fields: dict[str, FieldDecision] = field(default_factory=dict)
    base_row: dict[str, Any] | None = None
    ours_row: dict[str, Any] | None = None
    theirs_row: dict[str, Any] | None = None
    conflicting_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "classification": self.classification,
            "conflict": self.conflict,
            "reason": self.reason,
            "merged": self.merged,
            "deleted": self.deleted,
            "fields": {name: d.to_dict() for name, d in self.fields.items()},
            "base_row": self.base_row,
            "ours_row": self.ours_row,
            "theirs_row": self.theirs_row,
            "conflicting_fields": list(self.conflicting_fields),
        }


@dataclass
class ConflictRecord:
    """未决/已决冲突。resolution 为 None 表示未决。"""

    key: dict[str, Any]
    classification: str
    reason: str
    base_row: dict[str, Any] | None
    ours_row: dict[str, Any] | None
    theirs_row: dict[str, Any] | None
    resolution: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MergePlan:
    table: str
    primary_key: list[str]
    base_snapshot_id: str
    ours_snapshot_id: str
    theirs_snapshot_id: str
    entries: dict[str, MergeEntry]          # 键元组的稳定字符串 -> 判定
    conflicts: dict[str, ConflictRecord]    # 全部冲突（含已解决）

    @property
    def unresolved(self) -> dict[str, ConflictRecord]:
        return {k: c for k, c in self.conflicts.items() if c.resolution is None}

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "primary_key": list(self.primary_key),
            "base_snapshot_id": self.base_snapshot_id,
            "ours_snapshot_id": self.ours_snapshot_id,
            "theirs_snapshot_id": self.theirs_snapshot_id,
            "entries": {k: e.to_dict() for k, e in self.entries.items()},
            "conflicts": {k: c.to_dict() for k, c in self.conflicts.items()},
        }


# ---------------------------------------------------------------- 合并运行

@dataclass
class MergeRun:
    """一次三方合并的全部状态；解决方案绑定开启时的三方快照身份。"""

    run_id: str
    table: str
    status: str                          # MergeStatus 的值
    base_snapshot_id: str
    ours_snapshot_id: str
    theirs_snapshot_id: str
    ours_branch: str
    theirs_branch: str
    plan: MergePlan
    created_at: str
    committed_snapshot_id: str | None = None
    commit_message: str | None = None
    committed_at: str | None = None

    def binding(self) -> tuple[str, str, str]:
        return self.base_snapshot_id, self.ours_snapshot_id, self.theirs_snapshot_id
