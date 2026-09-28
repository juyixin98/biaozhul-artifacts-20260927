"""执行内核（kernel-1.0.0）：两级保守裁剪。

核心安全原则
============
分区值是原列经过 ``month`` 变换后的桶值（"2024-02"），**不是原 epoch 秒**。
因此本内核从不把桶值当作原值与谓词比较，而是从谓词的原值区间**反推**
"哪些桶可能含有满足谓词的值"（候选桶集合），并对开区间/边界不确定性保守扩张：

* 对整月闭区间谓词，候选桶是 [起点月, 终点月]，可安全裁掉区间外整桶；
* 对任意带"日/时分秒/开区间"不确定性的边界，相邻桶整月保留。

只有给出"该文件不可能匹配"的确定性证据时才裁剪；统计缺失、可能截断、
NULL 计数未知等情况一律保留。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import transforms as T
from .model import (
    Certainty, ColumnStats, Decision, FileEntry, Layer, PartitionEntry,
    Predicate, PredicateKind, PrunePlan, PruneReason, TableMetadata,
)
from .versions import KERNEL_VERSION


@dataclass
class CandidateBuckets:
    """谓词反推出的候选月桶闭区间 [first, last]（按 (year,month) 整数序）。"""
    first: tuple[int, int]
    last: tuple[int, int]


def _predicate_numeric_interval(pred: Predicate):
    """把谓词归一成原值（epoch 秒）半开数值区间用于文件统计比较。

    返回 (lo, lo_inc, hi, hi_inc)，None 表示无界。日期字符串按 UTC 转 epoch 边界，
    等值的日期自动扩张为整日区间（保守）。
    """
    kind = pred.kind

    if kind is PredicateKind.RANGE:
        return (pred.lower, pred.lower_inclusive, pred.upper, pred.upper_inclusive)
    if kind is PredicateKind.EQ:
        return (pred.value, True, pred.value, True)
    return None  # IN / NULL 单独处理


def _date_day_window(value):
    """YYYY-MM-DD -> [epoch_lo, epoch_hi_exclusive)；非日期字符串返回 None。"""
    if isinstance(value, str):
        try:
            return T.date_range_epoch_bounds(value, value)
        except ValueError:
            return None
    return None


def candidate_month_buckets(pred: Predicate) -> list[tuple[int, int]]:
    """从针对（可分区）列的谓词反推候选月桶列表。

    仅处理可桶化的 RANGE / EQ；IN 是多个 EQ 的并；NULL 不匹配任何非空桶。
    """
    kind = pred.kind

    if kind in (PredicateKind.IS_NULL, PredicateKind.NOT_NULL):
        return []  # 由调用方按分区非空性专门处理

    def buckets_for_scalar(v):
        win = _date_day_window(v)
        if win is not None:
            return [T.seconds_to_civil(win[0])[:2]]
        if isinstance(v, (int, float)):
            return [T.seconds_to_civil(float(v))[:2]]
        return None

    if kind is PredicateKind.EQ:
        b = buckets_for_scalar(pred.value)
        return b if b else []

    if kind is PredicateKind.IN:
        out: set[tuple[int, int]] = set()
        for v in (pred.values or ()):
            b = buckets_for_scalar(v)
            if b is None:
                return []  # 出现不可桶化的值 -> 不做分区裁剪
            out.update(b)
        return sorted(out)

    if kind is PredicateKind.RANGE:
        return _range_candidate_buckets(pred)

    return []


def _range_candidate_buckets(pred: Predicate) -> list[tuple[int, int]]:
    lo, lo_inc, hi, hi_inc = pred.lower, pred.lower_inclusive, pred.upper, pred.upper_inclusive

    def endpoint_month(v):
        win = _date_day_window(v)
        if win is not None:
            return T.seconds_to_civil(win[0])[:2]
        if isinstance(v, (int, float)):
            return T.seconds_to_civil(float(v))[:2]
        return None

    def is_month_start(v):
        win = _date_day_window(v)
        epoch = win[0] if win is not None else (v if isinstance(v, (int, float)) else None)
        if epoch is None:
            return False
        y, m, d = T.seconds_to_civil(float(epoch))
        return d == 1 and (int(epoch) % T._SECONDS_PER_DAY == 0)

    start = endpoint_month(lo) if lo is not None else None
    end = endpoint_month(hi) if hi is not None else None
    # 开区间上界若恰为某月首时刻，则该月桶可安全排除（值严格小于该时刻）
    if end is not None and hi is not None and not hi_inc and is_month_start(hi):
        end = T.add_months(end[0], end[1], -1)

    if start is None and end is None:
        return []
    lo_b = start or (-9999, 1)
    hi_b = end or (9999, 12)
    if lo_b > hi_b:
        return []
    y, m = lo_b
    res = []
    while (y, m) <= hi_b:
        res.append((y, m))
        y, m = T.add_months(y, m, 1)
    return res


# ---------------------------------------------------------------- 文件层

def _to_ts_window(value):
    """把等值/边界归一为数值；日期字符串 -> (lo,hi_exclusive) 整日窗口。"""
    win = _date_day_window(value)
    if win is not None:
        return win
    return None


def _numeric_predicate_prunes_stats(col: ColumnStats, pred: Predicate):
    """对数值/时间戳列做文件统计裁剪。返回 (pruned, reason, detail, evidence)。"""
    kind = pred.kind

    if kind is PredicateKind.IS_NULL:
        return _null_prunes(col, want_null=True)
    if kind is PredicateKind.NOT_NULL:
        return _null_prunes(col, want_null=False)

    if kind is PredicateKind.IN:
        if not col.present:
            return False, PruneReason.KEPT_STATS_MISSING, "统计缺失，IN 无法裁剪", {}
        windows = []
        for v in pred.values or ():
            w = _to_ts_window(v)
            windows.append(w if w is not None else (v, v))
        return _in_prunes(col, windows)

    # EQ：数值/时间戳点可精确比较；日期字符串走整日窗口（含整日语义）
    if kind is PredicateKind.EQ and not isinstance(pred.value, str):
        if not col.present:
            return False, PruneReason.KEPT_STATS_MISSING, "统计缺失，EQ 无法裁剪", {}
        v = pred.value
        if col.truncated:
            return False, PruneReason.KEPT_STATS_TRUNCATED, "统计可能截断，EQ 不裁剪", {}
        if col.max_present and col.max_value < v:
            return (True, PruneReason.STATS_EQ_NO_OVERLAP,
                    f"文件 max={col.max_value} < EQ 点 {v}", {"file_max": col.max_value, "eq": v})
        if col.min_present and col.min_value > v:
            return (True, PruneReason.STATS_EQ_NO_OVERLAP,
                    f"文件 min={col.min_value} > EQ 点 {v}", {"file_min": col.min_value, "eq": v})
        return False, PruneReason.KEPT_BY_PREDICATE, "EQ 点落在文件区间内", {}

    # RANGE / 日期型 EQ
    lo, lo_inc, hi, hi_inc = _predicate_numeric_interval(pred)
    # 仅当列是时间戳语义时，才允许把日期字面量边界换算成 epoch 整日窗口；
    # 普通 int64 列上出现日期字符串属于不可比，保守保留。
    is_ts_col = col.type == "timestamp"
    if not is_ts_col and isinstance(lo, str) or not is_ts_col and isinstance(hi, str):
        return False, PruneReason.KEPT_BY_PREDICATE, "日期字面量与非时间戳列不可比，保守保留", {}
    if is_ts_col:
        lo_w = _to_ts_window(lo) if lo is not None else None
        if lo_w is not None:
            lo, lo_inc = lo_w[0], True
        hi_w = _to_ts_window(hi) if hi is not None else None
        if hi_w is not None:
            hi, hi_inc = hi_w[1], False  # 次日 00:00 作为排他上界

    if not col.present:
        return False, PruneReason.KEPT_STATS_MISSING, "统计缺失，范围谓词无法裁剪", {}

    # 下界裁剪：文件最大值 < 谓词下界
    if lo is not None and col.max_present:
        if col.truncated:
            return False, PruneReason.KEPT_STATS_TRUNCATED, "max 统计可能截断，下界方向不裁剪", {}
        if (col.max_value < lo) or (not lo_inc and col.max_value <= lo):
            return (True, PruneReason.STATS_BELOW_LOWER,
                    f"文件 max={col.max_value} 不可能 >= 谓词下界 {lo}"
                    + ("（开界）" if not lo_inc else ""),
                    {"file_max": col.max_value, "pred_lower": lo, "lower_inclusive": lo_inc})
    elif lo is not None and not col.max_present:
        return False, PruneReason.KEPT_STATS_MISSING, "缺少 max 统计，无法按下界裁剪", {}

    # 上界裁剪：文件最小值 > 谓词上界
    if hi is not None and col.min_present:
        if col.truncated:
            return False, PruneReason.KEPT_STATS_TRUNCATED, "min 统计可能截断，上界方向不裁剪", {}
        if (col.min_value > hi) or (not hi_inc and col.min_value >= hi):
            return (True, PruneReason.STATS_ABOVE_UPPER,
                    f"文件 min={col.min_value} 不可能 <= 谓词上界 {hi}"
                    + ("（开界）" if not hi_inc else ""),
                    {"file_min": col.min_value, "pred_upper": hi, "upper_inclusive": hi_inc})
    elif hi is not None and not col.min_present:
        return False, PruneReason.KEPT_STATS_MISSING, "缺少 min 统计，无法按上界裁剪", {}

    return False, PruneReason.KEPT_BY_PREDICATE, "文件统计与谓词区间重叠", {}


def _null_prunes(col: ColumnStats, want_null: bool):
    if not col.present:
        return False, PruneReason.KEPT_STATS_MISSING, "统计缺失，NULL 谓词无法裁剪", {}
    if col.null_count is None:
        return False, PruneReason.KEPT_NULL_COUNT_UNKNOWN, "null_count 未知，NULL 谓词保守保留", {}
    if want_null:
        # IS NULL：文件一个 NULL 都没有 -> 裁剪
        if col.null_count == 0:
            return (True, PruneReason.STATS_NO_NULL_VS_IS_NULL,
                    "文件 null_count=0，不可能存在 NULL", {"null_count": 0})
    else:
        # NOT NULL：文件全是 NULL -> 裁剪
        if col.row_count > 0 and col.null_count >= col.row_count:
            return (True, PruneReason.STATS_ALL_NULL_VS_NOT_NULL,
                    f"文件 {col.null_count}/{col.row_count} 行全为 NULL，不可能非空",
                    {"null_count": col.null_count, "row_count": col.row_count})
    return False, PruneReason.KEPT_BY_PREDICATE, "文件可能含有所需 NULL 形态", {}


def _in_prunes(col: ColumnStats, windows):
    """windows: [(lo,hi_exclusive_or_eq)]；标量给 (v,v)。"""
    # 计算所有候选窗口的最小下界与最大上界；若文件区间整体在并集跨度之外则可裁。
    # （跨度之外是并集不相交的充分条件；跨度过松由 stats 重叠保守保留。）
    los = [w[0] for w in windows]
    his = [w[1] for w in windows]
    span_lo, span_hi = min(los), max(his)
    if col.truncated:
        return False, PruneReason.KEPT_STATS_TRUNCATED, "统计可能截断，IN 不裁剪", {}
    if col.max_present and col.max_value < span_lo:
        return (True, PruneReason.STATS_IN_NO_OVERLAP,
                f"文件 max={col.max_value} 小于 IN 最小候选 {span_lo}",
                {"file_max": col.max_value, "in_min": span_lo})
    if col.min_present and col.min_value > span_hi:
        return (True, PruneReason.STATS_IN_NO_OVERLAP,
                f"文件 min={col.min_value} 大于 IN 最大候选 {span_hi}",
                {"file_min": col.min_value, "in_max": span_hi})
    return False, PruneReason.KEPT_BY_PREDICATE, "文件统计与 IN 候选可能重叠", {}


def _string_predicate_prunes_stats(col: ColumnStats, pred: Predicate):
    kind = pred.kind
    if kind in (PredicateKind.IS_NULL, PredicateKind.NOT_NULL):
        return _null_prunes(col, want_null=(kind is PredicateKind.IS_NULL))
    if not col.present:
        return False, PruneReason.KEPT_STATS_MISSING, "字符串统计缺失，无法裁剪", {}
    if col.truncated:
        return False, PruneReason.KEPT_STATS_TRUNCATED, "字符串 min/max 可能被截断，保守保留", {}

    if kind is PredicateKind.EQ:
        v = pred.value
        if not isinstance(v, str):
            return False, PruneReason.KEPT_BY_PREDICATE, "值类型与字符串列不符，保守保留", {}
        below = col.max_present and col.max_value < v
        above = col.min_present and col.min_value > v
        if below or above:
            return (True, PruneReason.STATS_EQ_NO_OVERLAP,
                    f"等值 {v!r} 落在文件区间 [{col.min_value!r},{col.max_value!r}] 之外",
                    {"value": v, "file_min": col.min_value, "file_max": col.max_value})
        return False, PruneReason.KEPT_BY_PREDICATE, "等值落在文件区间内", {}

    if kind is PredicateKind.IN:
        vals = [v for v in (pred.values or ()) if isinstance(v, str)]
        if len(vals) != len(pred.values or ()):
            return False, PruneReason.KEPT_BY_PREDICATE, "IN 含非字符串值，保守保留", {}
        if vals and (col.max_present and max(vals) < col.min_value or
                     col.min_present and min(vals) > col.max_value):
            return (True, PruneReason.STATS_IN_NO_OVERLAP,
                    "IN 集合整体落在文件字符串区间之外",
                    {"in_values": vals[:20], "file_min": col.min_value, "file_max": col.max_value})
        return False, PruneReason.KEPT_BY_PREDICATE, "IN 与文件区间可能重叠", {}

    if kind is PredicateKind.RANGE:
        lo, hi = pred.lower, pred.upper
        # 下界裁剪：文件最大字符串都达不到谓词下界
        if lo is not None:
            if not isinstance(lo, str):
                return False, PruneReason.KEPT_BY_PREDICATE, "下界非字符串，保守保留", {}
            if col.max_present and (col.max_value < lo or
                                    (not pred.lower_inclusive and col.max_value <= lo)):
                return (True, PruneReason.STATS_BELOW_LOWER,
                        f"文件 max={col.max_value!r} 不可能满足 "
                        f"{'>' if not pred.lower_inclusive else '>='} {lo!r}",
                        {"file_max": col.max_value, "pred_lower": lo,
                         "lower_inclusive": pred.lower_inclusive})
        # 上界裁剪：文件最小字符串都超过谓词上界
        if hi is not None:
            if not isinstance(hi, str):
                return False, PruneReason.KEPT_BY_PREDICATE, "上界非字符串，保守保留", {}
            if col.min_present and (col.min_value > hi or
                                    (not pred.upper_inclusive and col.min_value >= hi)):
                return (True, PruneReason.STATS_ABOVE_UPPER,
                        f"文件 min={col.min_value!r} 不可能满足 "
                        f"{'<' if not pred.upper_inclusive else '<='} {hi!r}",
                        {"file_min": col.min_value, "pred_upper": hi,
                         "upper_inclusive": pred.upper_inclusive})
        return False, PruneReason.KEPT_BY_PREDICATE, "字符串区间与文件重叠", {}

    return False, PruneReason.KEPT_BY_PREDICATE, "未支持的字符串谓词，保守保留", {}


# ---------------------------------------------------------------- 计划


class PruningKernel:
    def __init__(self, transform_version: str):
        self.transform_version = transform_version

    def plan(self, metadata: TableMetadata, predicates: list[Predicate],
             request_id: str) -> PrunePlan:
        plan = PrunePlan(request_id=request_id, table=metadata.table,
                         predicates=list(predicates), transform_version=self.transform_version)
        plan.kernel_version = KERNEL_VERSION

        pcol = metadata.partition_column
        part_preds = [p for p in predicates if p.column == pcol]

        total_files = sum(len(p.files) for p in metadata.partitions)
        total_rows = sum(f.row_count for _, f in metadata.iter_files())

        # 候选桶集合：多个分区谓词之间为 AND -> 取交集。
        # 注意：IS NULL / NOT NULL 不能在分区层裁剪——月桶是值的变换，
        # NULL 行不产生任何桶值，其物理位置无法由分区目录推出；交由文件
        # stats 的 null_count 在第二层判定。
        candidate_set = None
        for p in part_preds:
            if p.kind in (PredicateKind.IS_NULL, PredicateKind.NOT_NULL):
                continue
            buckets = candidate_month_buckets(p)
            s = set(buckets)
            candidate_set = s if candidate_set is None else (candidate_set & s)

        # ---- 第一层：目录分区裁剪 ----
        surviving_partitions: list[PartitionEntry] = []
        for part in metadata.partitions:
            pruned, reason, detail, evidence = self._judge_partition(
                part, candidate_set, part_preds)
            if pruned:
                plan.decisions.append(Decision(
                    "partition", f"{pcol}={part.value}", Layer.PARTITION,
                    Certainty.PRUNED, reason, detail, pcol, evidence))
                for f in part.files:
                    plan.decisions.append(Decision(
                        "file", f.file_id, Layer.PARTITION, Certainty.PRUNED,
                        reason, f"随分区 {part.value} 整体裁掉：{detail}", pcol, evidence))
            else:
                plan.decisions.append(Decision(
                    "partition", f"{pcol}={part.value}", Layer.PARTITION,
                    Certainty.KEPT, reason, detail, pcol, evidence))
                surviving_partitions.append(part)

        # ---- 第二层：文件统计裁剪（逐谓词，AND 语义）----
        for part in surviving_partitions:
            for f in part.files:
                decision = self._judge_file(metadata, f, predicates, part.value)
                plan.decisions.append(decision)
                if decision.certainty is Certainty.KEPT:
                    plan.selected_files.append(f.file_id)

        # ---- 分层裁剪量统计 ----
        files_after_partition = sum(len(p.files) for p in surviving_partitions)
        pruned_partition = total_files - files_after_partition
        pruned_stats = files_after_partition - len(plan.selected_files)
        plan.totals = {
            "files_total": total_files,
            "rows_total": total_rows,
            "partitions_total": len(metadata.partitions),
            "partitions_surviving": len(surviving_partitions),
            "files_pruned_by_partition": pruned_partition,
            "files_pruned_by_stats": pruned_stats,
            "files_selected": len(plan.selected_files),
            "rows_selected": sum(
                f.row_count for _, f in metadata.iter_files()
                if f.file_id in set(plan.selected_files)),
        }
        # 不确定性单列
        kept_missing = [d.target_id for d in plan.decisions
                        if d.reason is PruneReason.KEPT_STATS_MISSING]
        kept_trunc = [d.target_id for d in plan.decisions
                      if d.reason is PruneReason.KEPT_STATS_TRUNCATED]
        kept_nullunk = [d.target_id for d in plan.decisions
                        if d.reason is PruneReason.KEPT_NULL_COUNT_UNKNOWN]
        if kept_missing:
            plan.notes.append(f"{len(kept_missing)} 个目标因统计缺失保留: {kept_missing}")
        if kept_trunc:
            plan.notes.append(f"{len(kept_trunc)} 个目标因统计可能截断保留: {kept_trunc}")
        if kept_nullunk:
            plan.notes.append(f"{len(kept_nullunk)} 个目标因 null_count 未知保留: {kept_nullunk}")
        return plan

    def _judge_partition(self, part: PartitionEntry, candidate_set, part_preds):
        if not part_preds:
            return (False, PruneReason.KEPT_NO_PREDICATE,
                    "分区列上无谓词，全部保留", {})
        if candidate_set is None:
            # 仅 IS NULL / NOT NULL 等：分区层不可判定，交文件 stats
            return (False, PruneReason.KEPT_NO_PREDICATE,
                    "NULL 类谓词无法由月桶目录判定，分区层保留，交文件 null_count", {})
        if not candidate_set:
            return (True, PruneReason.PARTITION_NO_MATCH_IN_LIST,
                    "候选桶集合为空（如等值/IN 与该桶不相交）",
                    {"partition_value": part.value})
        try:
            ym = T.parse_month_key(part.value)
        except ValueError:
            return (False, PruneReason.KEPT_PARTITION_OVERLAPS,
                    "分区桶值无法按固定变换解析，保守保留",
                    {"partition_value": part.value})
        if ym in candidate_set:
            return (False, PruneReason.KEPT_PARTITION_OVERLAPS,
                    f"桶 {part.value} 属于谓词反推出的候选月桶集合",
                    {"partition_value": part.value,
                       "candidate_buckets": sorted(
                           T.month_key(y, m) for y, m in candidate_set)})
        # 给出"不可能匹配"的具体理由
        cands = sorted(candidate_set)
        nearest_lo = T.month_key(*cands[0])
        nearest_hi = T.month_key(*cands[-1])
        if ym < cands[0]:
            return (True, PruneReason.PARTITION_OUTSIDE_RANGE,
                    f"桶 {part.value} 早于最早候选桶 {nearest_lo}，"
                    f"桶内所有原值均 < 谓词下界（基于 {self.transform_version} 反推）",
                    {"partition_value": part.value, "candidate_first": nearest_lo})
        return (True, PruneReason.PARTITION_OUTSIDE_RANGE,
                f"桶 {part.value} 晚于最晚候选桶 {nearest_hi}，"
                f"桶内所有原值均 > 谓词上界（基于 {self.transform_version} 反推）",
                {"partition_value": part.value, "candidate_last": nearest_hi})

    def _judge_file(self, metadata: TableMetadata, f: FileEntry,
                    predicates, part_value: str) -> Decision:
        kept = None
        for pred in predicates:
            col = f.stats.get(pred.column)
            if col is None:
                return Decision(
                    "file", f.file_id, Layer.FILE_STATS, Certainty.KEPT,
                    PruneReason.KEPT_STATS_MISSING,
                    f"列 {pred.column} 无统计信息，保守保留文件 {f.physical_path}",
                    pred.column, {"partition_value": part_value})
            ctype = col.type
            if ctype == "string":
                pruned, reason, detail, ev = _string_predicate_prunes_stats(col, pred)
            else:
                pruned, reason, detail, ev = _numeric_predicate_prunes_stats(col, pred)
            ev = dict(ev, partition_value=part_value)
            if pruned:
                return Decision(
                    "file", f.file_id, Layer.FILE_STATS, Certainty.PRUNED,
                    reason, f"{f.physical_path}: {detail}", pred.column, ev)
            kept = Decision(
                "file", f.file_id, Layer.FILE_STATS, Certainty.KEPT,
                reason, f"{f.physical_path}: {detail}", pred.column, ev)
        return kept or Decision(
            "file", f.file_id, Layer.FILE_STATS, Certainty.KEPT,
            PruneReason.KEPT_NO_PREDICATE,
            f"无谓词约束，保留文件 {f.physical_path}", None,
            {"partition_value": part_value})
