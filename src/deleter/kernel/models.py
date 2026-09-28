"""领域数据模型（纯 dataclass，不含任何 IO / 框架依赖）。

模块间数据流：
    adapters  -> RawIngest/Arrow 数据
    kernel    <- FileData / DeleteOp（由 metadata 组装）
    kernel    -> ScanReport（逐行判定）
    metadata  <-> sqlite 持久化
    api       <- Pydantic schema 与领域模型互转
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 删除类型与原因码是跨层稳定字符串，常量集中在此。
POSITION = "position"   # 文件行号删除
EQUALITY = "equality"   # 主键等值删除

KEEP = "keep"
DELETE = "delete"

# 保留原因
KEEP_INSCOPE_INSERT = "in_scope_insert"            # 晚于删除序号插入，删除不可见
KEEP_NO_MATCH = "no_match"                          # 无任何等值删除命中
KEEP_NULL_KEY_BLOCKED = "null_key_blocked"          # 谓词含 NULL，NULL 不等于任何值
KEEP_NULL_ROW_BLOCKED = "null_row_key_blocked"      # 行键含 NULL，不与任何谓词匹配

# 删除原因
DEL_POSITION = "position_delete"
DEL_EQUALITY = "equality_delete"

# 失效位置删除的处置（不出现在逐行 verdict 中，单独 op_evaluation 列出）
STALE_REWRITTEN = "stale_file_rewritten"            # 目标文件已被重写
STALE_ROW_REMOVED = "stale_row_already_removed"     # 目标行在旧版本里已不存在
STALE_OUT_OF_RANGE = "stale_out_of_range"           # 旧版本本就没有该行号

# 等值删除对操作本身的评估
OP_APPLIED = "applied"                  # 删除了至少一行
OP_APPLIED_ZERO = "applied_zero_rows"   # 注册成功但当前无可见命中
OP_IDEMPOTENT = "idempotent_duplicate"  # 同一 delete_id 重复提交，原样返回


@dataclass(frozen=True)
class Schema:
    """列名 -> Arrow 类型名（字符串形式，跨层不直接传 pyarrow 对象）。"""

    columns: dict[str, str]


@dataclass(frozen=True)
class KeyValue:
    """等值谓词中一个键组件的值；值为 None 表示 SQL NULL。"""

    columns: tuple[str, ...]
    values: tuple[Any, ...]


@dataclass
class DeleteOp:
    """一条已注册的删除操作（执行内核的输入）。"""

    delete_id: str
    kind: str                          # POSITION / EQUALITY
    seq: int                           # 全局单调序号 = 可见性序列号
    # 位置删除专用
    file_id: str | None = None
    row_number: int | None = None      # 0 基行号
    bound_version: int | None = None   # 绑定时文件版本（内容身份）
    # 等值删除专用
    key_columns: tuple[str, ...] = ()
    key_values: tuple[Any, ...] = ()

    @property
    def has_null_predicate(self) -> bool:
        return self.kind == EQUALITY and any(v is None for v in self.key_values)


@dataclass
class Row:
    """文件中的一行（内存表示）。"""

    values: dict[str, Any]
    insert_seq: int                    # 进入本表的序列号（可见性判断依据）


@dataclass
class FileData:
    """执行内核视角的一个文件版本快照。"""

    file_id: str
    version: int                       # 当前内容版本
    rows: list[Row]
    columns: tuple[str, ...]


@dataclass
class RowVerdict:
    """单行的最终处置与依据。"""

    file_id: str
    row_number: int                    # 当前文件版本中的 0 基行号
    insert_seq: int
    action: str                        # KEEP / DELETE
    reason: str
    by_delete_id: str | None = None
    by_seq: int | None = None
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpEvaluation:
    """一次扫描对每个删除操作的逐条评估（可重放依据）。"""

    delete_id: str
    kind: str
    seq: int
    status: str                        # applied / applied_zero_rows / stale_* / idempotent_duplicate
    matched_rows: list[tuple[str, int]] = field(default_factory=list)  # (file_id, row_number)


@dataclass
class ScanReport:
    """一次完整扫描（跨全部 live 文件）的逐行结论。"""

    table_id: str
    seq_horizon: int                   # 扫描时已分配的最大序号
    verdicts: list[RowVerdict]
    op_evaluations: list[OpEvaluation]
    files: dict[str, int] = field(default_factory=dict)  # file_id -> version

    def kept_rows(self) -> list[RowVerdict]:
        return [v for v in self.verdicts if v.action == KEEP]

    def deleted_rows(self) -> list[RowVerdict]:
        return [v for v in self.verdicts if v.action == DELETE]
