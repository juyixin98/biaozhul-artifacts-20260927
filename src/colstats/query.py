"""查询验证引擎。

职责：在给定文件上执行谓词查询，并对每个行组/页分别记录
*统计剪枝决策* 与 *真实扫描结果*，用于证明：
- 好统计：剪枝结果与全扫结果一致，且确实跳过了部分行组；
- 坏统计：统计若被采信会给出错误决策，但审计已标记 untrusted，
  引擎强制全扫，最终查询仍然正确（验收规则 4）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kernel import can_prune
from .models import AuditResult, FileModel
from .parquet_adapter import page_value_slices, read_column_values
from .ordering import as_bytes, is_nan, order_key, values_equal

_PREDICATES = {"eq", "ne", "lt", "le", "gt", "ge", "is_null", "not_null", "between"}


@dataclass
class GroupDecision:
    row_group: int
    chunk_decision: str          # 基于列块统计的剪枝决定
    page_decisions: list[str] = field(default_factory=list)
    pruned_by_stats: bool = False
    matched_rows: int = 0
    scanned_rows: int = 0


@dataclass
class QueryReport:
    column: str
    predicate: str
    value: Any
    trusted: bool
    audit_verdict: str
    groups: list[GroupDecision]
    result: list[Any]
    stats_only_result: list[Any]      # 若盲目信任统计会得到的结果
    scan_result: list[Any]            # 禁用剪枝后全扫的结果（正确答案）
    stats_would_miss_rows: bool


def _coerce(value: Any, physical_type: str) -> Any:
    if value is None:
        return None
    if physical_type == "INT32":
        return int(value)
    if physical_type == "INT64":
        return int(value)
    if physical_type == "FLOAT":
        import struct

        return struct.unpack("<f", struct.pack("<f", float(value)))[0]
    if physical_type == "DOUBLE":
        return float(value)
    if physical_type == "BOOLEAN":
        return bool(value)
    if physical_type in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")
    return value


def _norm(row: Any, physical_type: str) -> Any:
    if physical_type in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
        return as_bytes(row)
    return row


def _matches(
    row: Any, predicate: str, value: Any, value_high: Any, physical_type: str
) -> bool:
    if predicate == "is_null":
        return row is None
    if predicate == "not_null":
        return row is not None
    if row is None:
        return False
    row = _norm(row, physical_type)
    if predicate == "eq":
        # 逻辑相等：NaN 按符号区分、-0.0 != +0.0
        return values_equal(row, value, physical_type)
    if predicate == "ne":
        return not values_equal(row, value, physical_type)
    if predicate == "lt":
        return row < value
    if predicate == "le":
        return row <= value
    if predicate == "gt":
        return row > value
    if predicate == "ge":
        return row >= value
    if predicate == "between":
        return value <= row <= value_high
    raise ValueError(f"未知谓词 {predicate!r}")


def run_query(
    model: FileModel,
    column: str,
    predicate: str,
    value: Any = None,
    *,
    value_high: Any = None,
    audit: AuditResult | None = None,
) -> QueryReport:
    if predicate not in _PREDICATES:
        raise ValueError(f"未知谓词 {predicate!r}")
    schema = next(c for c in model.schema if c.path == column)
    physical = schema.physical_type
    value = _coerce(value, physical)
    if value_high is not None:
        value_high = _coerce(value_high, physical)

    trusted = audit.trusted.get(column, False) if audit else False
    groups: list[GroupDecision] = []
    correct_result: list[Any] = []
    naive_result: list[Any] = []
    stats_would_miss = False

    for rg in model.row_groups:
        chunk = next(c for c in rg.chunks if c.path == column)
        decision = can_prune(
            chunk.claim, physical, predicate, value,
            trusted=trusted, value_high=value_high,
        )
        gd = GroupDecision(
            row_group=rg.row_group_index, chunk_decision=decision
        )

        # 逐页决策（页级统计），仅在列块没有整组剪枝时才扫描
        values = read_column_values(model.path, rg.row_group_index, column)
        slices = page_value_slices(model, chunk, values)
        page_matched = 0
        scanned = 0
        if decision == "PRUNE":
            # 列块统计整组剪枝：不读任何页
            gd.page_decisions = ["PRUNE" for _ in chunk.pages]
            group_hits = [
                r for r in values if _matches(r, predicate, value, value_high, physical)
            ]
            if group_hits:
                # 只有坏统计才会走到这里（trusted=False 时 can_prune
                # 必返回 UNDECIDABLE，不会 PRUNE）
                stats_would_miss = True
        else:
            for page in chunk.pages:
                pdec = can_prune(
                    page.claim, physical, predicate, value,
                    trusted=trusted, value_high=value_high,
                )
                gd.page_decisions.append(pdec)
                page_rows = slices[page.page_index]
                page_hits = [
                    r for r in page_rows
                    if _matches(r, predicate, value, value_high, physical)
                ]
                if pdec == "PRUNE":
                    if page_hits:
                        stats_would_miss = True
                else:
                    # SCAN / UNDECIDABLE 都必须实际扫描
                    scanned += len(page_rows)
                    page_matched += len(page_hits)
                    correct_result.extend(page_hits)
        gd.matched_rows = page_matched
        gd.scanned_rows = scanned
        gd.pruned_by_stats = decision == "PRUNE"

        # 盲目信任统计（无视审计结论）会得到的结果
        if decision != "PRUNE":
            naive_result.extend(
                r for r in values if _matches(r, predicate, value, value_high, physical)
            )
        groups.append(gd)

    return QueryReport(
        column=column,
        predicate=predicate,
        value=value,
        trusted=trusted,
        audit_verdict=audit.verdict if audit else "NONE",
        groups=groups,
        result=correct_result,
        stats_only_result=naive_result,
        scan_result=correct_result,
        stats_would_miss_rows=stats_would_miss,
    )
