"""独立验证层（validation oracle）。

参考真值 **不使用裁剪内核**，而是用 PyArrow Compute 对每个文件逐行全扫描，
构造布尔 mask 独立判断该文件是否真的含有满足全部谓词（AND）的行。

随后与内核裁剪计划对比：
* 任何被裁掉却实际含匹配行的文件 -> MISSED_MATCH（零漏行失败，最严重）；
* 含匹配行但未被选中 -> MISSED_MATCH；
* 结构/谓词/统计问题 -> 对应失败类别。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .model import Certainty, Predicate, PredicateKind, PrunePlan
from . import transforms as T


class FailureCategory:
    MISSED_MATCH = "missed_match"                 # 零漏行：裁剪漏掉真实匹配
    MISSING_FILE = "missing_file"                 # 计划引用了磁盘上不存在的文件
    TYPE_MISMATCH = "type_mismatch"               # 谓词与列类型不符
    UNKNOWN_COLUMN = "unknown_column"             # 谓词引用不存在的列
    SELECTED_BUT_EMPTY_MATCH = "selected_no_match"  # 选中但无匹配（仅冗余，不算正确性失败）


@dataclass
class FileScanResult:
    file_id: str
    exists: bool
    row_count: int
    matched_rows: int
    scanned: bool


@dataclass
class ValidationReport:
    request_id: str
    status: str = "pending"          # "pass" | "fail"
    failures: list[dict] = field(default_factory=list)
    file_scans: list[dict] = field(default_factory=list)
    rows_scanned: int = 0
    rows_matched_full_scan: int = 0
    zero_missed_matches: bool = True
    kernel_selected: list[str] = field(default_factory=list)
    truly_matching_files: list[str] = field(default_factory=list)
    layers: dict = field(default_factory=dict)
    date_transform: str = T.DATE_TRANSFORM_VERSION

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "zero_missed_matches": self.zero_missed_matches,
            "rows_scanned": self.rows_scanned,
            "rows_matched_full_scan": self.rows_matched_full_scan,
            "kernel_selected_files": self.kernel_selected,
            "truly_matching_files": self.truly_matching_files,
            "layer_pruning": self.layers,
            "failures": self.failures,
            "file_scans": self.file_scans,
            "date_transform": self.date_transform,
        }


def _all(masks):
    """对若干布尔 ChunkedArray 做 AND；空列表返回 None（调用方按无约束处理）。"""
    masks = [m for m in masks if m is not None]
    if not masks:
        return None
    acc = masks[0]
    for m in masks[1:]:
        acc = pc.and_(acc, m)
    return acc


def _all_or(masks):
    masks = [m for m in masks if m is not None]
    if not masks:
        return None
    acc = masks[0]
    for m in masks[1:]:
        acc = pc.or_(acc, m)
    return acc


def _to_timestamp_scalar(value):
    if isinstance(value, str):
        lo, hi = T.date_range_epoch_bounds(value, value)
        return lo, hi  # 整日窗口
    return value, value


def _to_seconds(col: pa.ChunkedArray) -> pa.ChunkedArray:
    """把 timestamp 列独立换算成 UTC epoch 秒（不与内核共享代码）。

    timestamp[us] 直接 cast 成 int64 会得到微秒，必须先降到秒单位。
    """
    if pa.types.is_timestamp(col.type):
        secs = pc.cast(col, pa.timestamp("s", tz="UTC"))
        return pc.cast(secs, pa.int64())
    return col  # 已是整数秒列


def _mask_for_timestamp(col: pa.ChunkedArray, pred: Predicate):
    kind = pred.kind
    secs = _to_seconds(col)
    if kind is PredicateKind.IS_NULL:
        return pc.is_null(secs)
    if kind is PredicateKind.NOT_NULL:
        return pc.is_valid(secs)

    def win(v):
        lo, hi = _to_timestamp_scalar(v)
        return lo, hi

    if kind is PredicateKind.EQ:
        lo, hi = win(pred.value)
        if lo != hi:  # 日期整日窗口
            return pc.and_(pc.greater_equal(secs, lo), pc.less(secs, hi))
        return pc.equal(secs, lo)
    if kind is PredicateKind.IN:
        masks = []
        for v in pred.values:
            lo, hi = win(v)
            masks.append(pc.and_(pc.greater_equal(secs, lo), pc.less(secs, hi))
                         if lo != hi else pc.equal(secs, lo))
        return _all_or(masks)
    if kind is PredicateKind.RANGE:
        masks = []
        if pred.lower is not None:
            lo, hi = win(pred.lower)
            bound = lo
            masks.append(pc.greater_equal(secs, bound) if pred.lower_inclusive
                         else pc.greater(secs, bound))
        if pred.upper is not None:
            lo, hi = win(pred.upper)
            if hi != lo:
                masks.append(pc.less(secs, hi))  # 日期上界 -> 次日 00:00 排他
            else:
                masks.append(pc.less_equal(secs, lo) if pred.upper_inclusive
                             else pc.less(secs, lo))
        return _all(masks)
    raise ValueError(f"不支持的谓词 {kind}")


def _mask_for_string(col: pa.ChunkedArray, pred: Predicate):
    kind = pred.kind
    if kind is PredicateKind.IS_NULL:
        return pc.is_null(col)
    if kind is PredicateKind.NOT_NULL:
        return pc.is_valid(col)
    if kind is PredicateKind.EQ:
        return pc.equal(col, pa.scalar(pred.value, type=pa.string()))
    if kind is PredicateKind.IN:
        return pc.is_in(col, value_set=pa.array(list(pred.values), type=pa.string()))
    if kind is PredicateKind.RANGE:
        masks = []
        if pred.lower is not None:
            op = pc.greater_equal if pred.lower_inclusive else pc.greater
            masks.append(op(col, pa.scalar(pred.lower, type=pa.string())))
        if pred.upper is not None:
            op = pc.less_equal if pred.upper_inclusive else pc.less
            masks.append(op(col, pa.scalar(pred.upper, type=pa.string())))
        return _all(masks)
    raise ValueError(f"不支持的谓词 {kind}")


class FullScanValidator:
    def validate(self, metadata, predicates: list[Predicate], plan: PrunePlan,
                 request_id: str) -> ValidationReport:
        report = ValidationReport(request_id=request_id,
                                  kernel_selected=list(plan.selected_files))
        known_cols = set(metadata.columns)

        for pred in predicates:
            if pred.column not in known_cols:
                report.failures.append({
                    "category": FailureCategory.UNKNOWN_COLUMN,
                    "column": pred.column,
                    "message": f"谓词引用了未注册的列 {pred.column}"})

        selected = set(plan.selected_files)
        truly = set()
        for part, f in metadata.iter_files():
            import os
            exists = os.path.exists(f.physical_path)
            if not exists:
                report.failures.append({
                    "category": FailureCategory.MISSING_FILE, "file_id": f.file_id,
                    "message": f"物理文件不存在: {f.physical_path}"})
                report.file_scans.append(FileScanResult(
                    f.file_id, False, f.row_count, 0, False).__dict__)
                continue

            table = pq.read_table(f.physical_path)
            masks = []
            local_fail = None
            for pred in predicates:
                if pred.column not in table.column_names:
                    local_fail = {
                        "category": FailureCategory.UNKNOWN_COLUMN,
                        "file_id": f.file_id, "column": pred.column,
                        "message": f"文件 {f.file_id} 缺少列 {pred.column}"}
                    break
                col = table.column(pred.column)
                ctype = f.stats.get(pred.column).type if f.stats.get(pred.column) else None
                if pa.types.is_timestamp(col.type) or pa.types.is_integer(col.type):
                    m = _mask_for_timestamp(col, pred)
                elif pa.types.is_string(col.type):
                    m = _mask_for_string(col, pred)
                else:
                    local_fail = {
                        "category": FailureCategory.TYPE_MISMATCH, "file_id": f.file_id,
                        "column": pred.column,
                        "message": f"列 {pred.column} 类型 {col.type} 不支持谓词"}
                    break
                if m is not None:
                    masks.append(m)
            if local_fail:
                report.failures.append(local_fail)
                matched = 0
            else:
                mask = _all(masks)
                if mask is None:
                    matched = table.num_rows  # 无谓词
                else:
                    matched = int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)

            report.rows_scanned += table.num_rows
            report.rows_matched_full_scan += matched
            report.file_scans.append({
                "file_id": f.file_id, "exists": True,
                "row_count": table.num_rows, "matched_rows": matched, "scanned": True})
            if matched > 0:
                truly.add(f.file_id)
                if f.file_id not in selected:
                    # 致命：内核裁掉了一个真正含匹配行的文件
                    report.zero_missed_matches = False
                    report.failures.append({
                        "category": FailureCategory.MISSED_MATCH,
                        "file_id": f.file_id, "matched_rows": matched,
                        "message": f"文件 {f.file_id} 含 {matched} 行匹配但被裁剪/漏选"})

        # 选中却无匹配：只是多读，不是正确性失败，单列提示
        for fid in selected - truly:
            report.failures.append({
                "category": FailureCategory.SELECTED_BUT_EMPTY_MATCH,
                "file_id": fid,
                "message": f"文件 {fid} 被选中但全扫描无匹配（保守冗余读取，非错误）"})

        report.truly_matching_files = sorted(truly)
        report.layers = {
            "files_pruned_by_partition": plan.totals.get("files_pruned_by_partition"),
            "files_pruned_by_stats": plan.totals.get("files_pruned_by_stats"),
            "files_selected": plan.totals.get("files_selected"),
            "files_total": plan.totals.get("files_total"),
        }
        hard = [f for f in report.failures
                if f["category"] != FailureCategory.SELECTED_BUT_EMPTY_MATCH]
        report.status = "fail" if hard else "pass"
        return report
