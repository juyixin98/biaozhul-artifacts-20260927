"""查询执行: 页级剪枝 + 行级求值; 无受信统计或坏统计时整页全扫。

查询结果永远以行级求值为准 —— 剪枝只用于减少 IO, 绝不会改变命中集合
(验收规则 4 的可查询正确性保证)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import adapter
from .audit import OK
from .logical import LogicalType, compare, is_nan
from .masking import mask_row
from .prune import (
    OP_IS_NULL,
    OP_NOT_NULL,
    Predicate,
    decide_page,
)


@dataclass
class QueryResult:
    dataset: str
    predicate: dict[str, Any]
    request_id: str
    total_rows: int
    matched_rows: int
    pages_total: int
    pages_scanned: int
    pages_skipped: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    page_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "request_id": self.request_id,
            "predicate": self.predicate,
            "total_rows": self.total_rows,
            "matched_rows": self.matched_rows,
            "pages_total": self.pages_total,
            "pages_scanned": self.pages_scanned,
            "pages_skipped": self.pages_skipped,
            "rows": self.rows,
            "page_trace": self.page_trace,
        }


def row_matches(
    value: Any, pred: Predicate, logical: LogicalType
) -> bool:
    if pred.op == OP_IS_NULL:
        return value is None
    if pred.op == OP_NOT_NULL:
        return value is not None
    # 三值逻辑: NULL 与 NaN 对比较谓词都不命中
    if value is None:
        return False
    return compare(pred.op, value, pred.value, logical)


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and is_nan(value):
        return "NaN"
    return value


def execute(
    ds: adapter.Dataset,
    pred: Predicate,
    page_verdicts: dict[tuple, dict[str, dict[str, Any]]],
    *,
    request_id: str,
    limit: int | None = None,
    redact: bool = True,
) -> QueryResult:
    """执行查询。

    page_verdicts: audit 之后的页级裁决表
      {(file, rg, page): {column: verdict_row}}
    谓词列在某页没有 ok 裁决 -> 该页整页扫描 (验收规则 4)。
    """
    logical_types = {c.name: c.logical_type for c in ds.columns}
    if pred.column not in logical_types:
        raise KeyError(f"谓词列不存在: {pred.column}")
    logical = logical_types[pred.column]

    result = QueryResult(
        dataset=ds.name,
        predicate={
            "column": pred.column,
            "op": pred.op,
            "value": _jsonable(pred.value),
        },
        request_id=request_id,
        total_rows=0,
        matched_rows=0,
        pages_total=0,
        pages_scanned=0,
        pages_skipped=0,
    )
    sensitive = set(ds.sensitive_columns) if redact else set()

    for fi in ds.files:
        for rg in fi.row_groups:
            # 一次读入行组全部列, 按页切片, 避免重复读 Parquet
            rg_columns = {
                c.name: adapter.read_row_group_values(
                    ds, fi.file, rg.index, c.name
                )
                for c in ds.columns
            }
            offset = 0
            for page in rg.pages:
                result.pages_total += 1
                key = (fi.file, rg.index, page.index)
                verdict_row = page_verdicts.get(key, {}).get(pred.column)

                action = "scanned"
                reason = "默认全扫"
                trusted = False
                if verdict_row is not None and verdict_row["verdict"] == OK:
                    trusted = True
                    claimed = ds.claimed_page_stats(
                        fi.file, rg.index, page.index, pred.column
                    )
                    if claimed is None:  # 防御: ok 裁决必有声明
                        action = "scanned_untrusted"
                        reason = "受信裁决缺失声明对象, 回退全扫"
                        result.pages_scanned += 1
                    else:
                        decision = decide_page(
                            pred,
                            claimed,
                            file=fi.file,
                            row_group=rg.index,
                            page=page.index,
                        )
                        if decision.skipped:
                            action = "skipped_pruned"
                            reason = decision.reason
                            result.pages_skipped += 1
                        else:
                            action = "scanned"
                            reason = decision.reason
                            result.pages_scanned += 1
                else:
                    v = verdict_row["verdict"] if verdict_row else "no_verdict"
                    action = "scanned_untrusted"
                    reason = (
                        f"统计裁决为 {v} (非 ok), 按规则 4 禁用剪枝, 全扫"
                    )
                    result.pages_scanned += 1

                result.total_rows += page.row_count
                if action != "skipped_pruned":
                    for i in range(offset, offset + page.row_count):
                        value = rg_columns[pred.column][i]
                        if row_matches(value, pred, logical):
                            row = {
                                c.name: rg_columns[c.name][i]
                                for c in ds.columns
                            }
                            if redact:
                                row = mask_row(row, sensitive)
                            result.rows.append(row)
                result.page_trace.append({
                    "file": fi.file,
                    "row_group": rg.index,
                    "page": page.index,
                    "action": action,
                    "reason": reason,
                    "rows_in_page": page.row_count,
                    "trusted_verdict": trusted,
                })
                offset += page.row_count

    result.matched_rows = len(result.rows)
    if limit is not None:
        result.rows = result.rows[:limit]
    return result


def full_scan_counts(
    ds: adapter.Dataset, pred: Predicate
) -> tuple[int, int]:
    """无任何统计假设的基线扫描, 返回 (总行数, 命中行数), 供测试/演示对照。"""
    logical = ds.column(pred.column).logical_type
    total = matched = 0
    for fi in ds.files:
        for rg in fi.row_groups:
            values = adapter.read_row_group_values(
                ds, fi.file, rg.index, pred.column
            )
            for v in values:
                total += 1
                if row_matches(v, pred, logical):
                    matched += 1
    return total, matched
