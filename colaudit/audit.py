"""审计执行内核 (验收规则 3、4)。

裁决 verdict:
* ok                  接受: 声明统计与重算事实逐字段一致, 可用于剪枝
* stats_missing       无法判定: 该层无声明统计 (拒绝剪枝, 但不是"错误")
* null_count_mismatch 拒绝: NULL 计数错误
* minmax_mismatch     拒绝: min/max 端点错误
* count_mismatch      拒绝: 行数错误
* nan_mismatch        拒绝: NaN 计数错误
* signed_zero_mismatch 拒绝: 有符号零标志错误
* truncation_invalid  拒绝: 截断声明与真实端点不相容 (可信区间被破坏)
* truncation_mismatch 拒绝: 截断标志错误
* sorted_mismatch     拒绝: 有序性声明错误
* aggregation_mismatch 拒绝: 行组统计 != 其页级统计的聚合
* embedded_conflict   拒绝: 声明统计与 Parquet 内嵌统计冲突
* embedded_inconclusive 无法判定: 内嵌统计自身缺失/不可表达, 无第二来源

只有 verdict == "ok" 的统计标记 trusted=True; 其余一律不得用于剪枝。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import adapter, masking
from .logical import LogicalType, endpoint_equal, sort_key
from .stats import ColumnStats, aggregate_stats, compute_column_stats, stats_equal

ACCEPT = "accept"
REJECT = "reject"
UNKNOWN = "unknown"

OK = "ok"
STATS_MISSING = "stats_missing"
NULL_COUNT_MISMATCH = "null_count_mismatch"
MINMAX_MISMATCH = "minmax_mismatch"
COUNT_MISMATCH = "count_mismatch"
NAN_MISMATCH = "nan_mismatch"
SIGNED_ZERO_MISMATCH = "signed_zero_mismatch"
TRUNCATION_INVALID = "truncation_invalid"
TRUNCATION_MISMATCH = "truncation_mismatch"
SORTED_MISMATCH = "sorted_mismatch"
AGGREGATION_MISMATCH = "aggregation_mismatch"
EMBEDDED_CONFLICT = "embedded_conflict"
EMBEDDED_INCONCLUSIVE = "embedded_inconclusive"

#: 无法判定的类别 (不允许剪枝, 但说明"为什么不能下结论")
INCONCLUSIVE = frozenset({STATS_MISSING, EMBEDDED_INCONCLUSIVE})

_PRIORITY = [
    COUNT_MISMATCH,
    NULL_COUNT_MISMATCH,
    NAN_MISMATCH,
    MINMAX_MISMATCH,
    TRUNCATION_INVALID,
    SIGNED_ZERO_MISMATCH,
    TRUNCATION_MISMATCH,
    SORTED_MISMATCH,
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DiagnosticEvent:
    code: str
    decision: str
    message: str
    state: dict[str, Any]
    file: str | None = None
    row_group: int | None = None
    scope: str | None = None
    page: int | None = None
    column_name: str | None = None
    request_id: str | None = None
    occurred_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "decision": self.decision,
            "message": self.message,
            "state": self.state,
            "file": self.file,
            "row_group": self.row_group,
            "scope": self.scope,
            "page": self.page,
            "column_name": self.column_name,
            "request_id": self.request_id,
            "occurred_at": self.occurred_at,
        }


class EventCollector:
    def __init__(self, run_id: str, request_id: str | None = None):
        self.run_id = run_id
        self.request_id = request_id
        self.events: list[DiagnosticEvent] = []

    def add(self, ev: DiagnosticEvent) -> None:
        if ev.request_id is None:
            ev.request_id = self.request_id
        self.events.append(ev)


def classify_diffs(
    actual: ColumnStats, claimed: ColumnStats
) -> list[str]:
    """把字段差异映射为有序失败类别列表 (优先级从高到低)。

    字符串列允许合法截断: 声明 min/max 是真实端点的前缀时不视为端点错误,
    但截断标志必须为 True; 端点的"精确相等"与"合法截断"都通过后,
    再用 _truncation_invalid 排除不相容声明。
    """
    failures: list[str] = []
    if actual.count != claimed.count:
        failures.append(COUNT_MISMATCH)
    if actual.null_count != claimed.null_count:
        failures.append(NULL_COUNT_MISMATCH)
    if actual.nan_count != claimed.nan_count:
        failures.append(NAN_MISMATCH)

    trunc_invalid = _truncation_invalid(actual, claimed)

    # 端点: 精确匹配或"声明截断且为真实端点合法前缀下界"均可接受
    end_bad = False
    for end, flag_name in (("min", "min_truncated"),
                           ("max", "max_truncated")):
        av, cv = getattr(actual, end), getattr(claimed, end)
        if av is None or cv is None:
            if av is not cv:
                end_bad = True
            continue
        if endpoint_equal(av, cv, actual.logical_type):
            continue
        if _is_valid_prefix_bound(av, cv, end, actual.logical_type,
                                  getattr(claimed, flag_name)):
            continue
        end_bad = True
    if trunc_invalid:
        failures.append(TRUNCATION_INVALID)
    elif end_bad:
        failures.append(MINMAX_MISMATCH)
    # 截断标志:
    # * 声明截断且确实构成合法前缀界 -> 接受 (真实端点本身不带截断标志,
    #   actual.*_truncated 恒为 False, 标志差异来自声明, 不算 mismatch);
    # * 声明截断但端点精确相等 (无需截断) 或非字符串列 -> 标志错误;
    # * 声明未截断却端点不精确 -> 已归入 MINMAX_MISMATCH。
    legit_prefix = {
        "min": _is_valid_prefix_bound(
            actual.min, claimed.min, "min", actual.logical_type,
            claimed.min_truncated,
        ),
        "max": _is_valid_prefix_bound(
            actual.max, claimed.max, "max", actual.logical_type,
            claimed.max_truncated,
        ),
    }
    for end, flag_name in (("min", "min_truncated"),
                           ("max", "max_truncated")):
        claimed_flag = getattr(claimed, flag_name)
        exact = (
            getattr(actual, end) is not None
            and getattr(claimed, end) is not None
            and endpoint_equal(
                getattr(actual, end), getattr(claimed, end),
                actual.logical_type,
            )
        )
        if claimed_flag and exact and not getattr(actual, flag_name):
            failures.append(TRUNCATION_MISMATCH)
        elif claimed_flag and not legit_prefix[end] and not trunc_invalid:
            # 非字符串列 / 非前缀却挂截断标志
            if actual.logical_type is not LogicalType.STRING or exact:
                failures.append(TRUNCATION_MISMATCH)
    if (
        actual.has_positive_zero != claimed.has_positive_zero
        or actual.has_negative_zero != claimed.has_negative_zero
    ):
        failures.append(SIGNED_ZERO_MISMATCH)
    if actual.sorted != claimed.sorted:
        failures.append(SORTED_MISMATCH)
    order = {c: i for i, c in enumerate(_PRIORITY)}
    failures.sort(key=lambda f: order.get(f, len(_PRIORITY)))
    return failures


def _is_valid_prefix_bound(
    actual_end: object,
    claimed_end: object,
    which: str,
    logical: LogicalType,
    claimed_truncated: bool,
) -> bool:
    """声明端点是否是真实端点的合法截断前缀界。"""
    if not claimed_truncated:
        return False
    if logical is not LogicalType.STRING:
        return False
    actual_s, claimed_s = str(actual_end), str(claimed_end)
    if not actual_s.startswith(claimed_s):
        return False
    if which == "min":
        # 前缀 <= 原串: 作为下界必须不大于真实 min
        return sort_key(claimed_s, LogicalType.STRING) <= sort_key(
            actual_s, LogicalType.STRING
        )
    # max: 前缀是"更短的串", 字典序上小于原串, 无法充当保守上界
    # -> 只有恰好相等时可用, 但相等已在 endpoint_equal 分支处理
    return False


def _truncation_invalid(actual: ColumnStats, claimed: ColumnStats) -> bool:
    """截断声明是否与真实端点不相容 (破坏可信区间)。

    合法的截断声明只可能出现在字符串列, 且必须满足:
    * 声明 min 是真实 min 的前缀 (claimed_min <= actual_min);
    * 声明 max 是真实 max 的前缀 (claimed_max >= actual_max, 即真实
      max 以声明 max 为前缀, 或声明 max 字典序更大);
    不相容意味着声明值落在真实区间错误的一侧 -> 剪枝会漏行。
    """
    if actual.logical_type is not LogicalType.STRING:
        # 非字符串列出现截断标志本身即不相容
        return claimed.min_truncated or claimed.max_truncated
    if claimed.min_truncated and claimed.min is not None and actual.min is not None:
        if not str(actual.min).startswith(str(claimed.min)):
            return True
        if sort_key(claimed.min, LogicalType.STRING) > sort_key(
            actual.min, LogicalType.STRING
        ):
            return True
    if claimed.max_truncated and claimed.max is not None and actual.max is not None:
        if not str(actual.max).startswith(str(claimed.max)):
            # 不是前缀时, 只有声明上界 >= 真实上界才可作为保守上界
            if sort_key(claimed.max, LogicalType.STRING) < sort_key(
                actual.max, LogicalType.STRING
            ):
                return True
    return False


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
def audit_dataset(
    ds: adapter.Dataset,
    *,
    request_id: str | None = None,
    run_id: str | None = None,
    mask_sensitive: bool = True,
) -> dict[str, Any]:
    """审计整个数据集, 返回可直接落库/序列化的报告 dict。"""
    run_id = run_id or uuid.uuid4().hex
    request_id = request_id or uuid.uuid4().hex
    collector = EventCollector(run_id, request_id)
    verdicts: list[dict[str, Any]] = []
    started = _utcnow()

    counters: dict[str, int] = {}

    for fi in ds.files:
        # 列 -> 该文件各页的重算事实, 供行组聚合校验复用
        for rg in fi.row_groups:
            actual_pages: dict[str, list[ColumnStats]] = {
                c.name: [] for c in ds.columns
            }
            claimed_pages: dict[str, list[ColumnStats | None]] = {
                c.name: [] for c in ds.columns
            }
            actual_rgs: dict[str, ColumnStats] = {}
            for col in ds.columns:
                rg_values = adapter.read_row_group_values(
                    ds, fi.file, rg.index, col.name
                )
                actual_rg = compute_column_stats(rg_values, col.logical_type)
                actual_rgs[col.name] = actual_rg

                offset = 0
                for page in rg.pages:
                    page_values = rg_values[offset : offset + page.row_count]
                    offset += page.row_count
                    actual = compute_column_stats(
                        page_values, col.logical_type
                    )
                    actual_pages[col.name].append(actual)
                    claimed = ds.claimed_page_stats(
                        fi.file, rg.index, page.index, col.name
                    )
                    claimed_pages[col.name].append(claimed)

                    verdict, failure, detail = _judge_page(
                        actual=actual,
                        claimed=claimed,
                        logical=col.logical_type,
                    )
                    _emit_page_events(
                        collector, ds, fi.file, rg.index, page.index,
                        col, actual, claimed, verdict, failure, detail,
                        mask_sensitive,
                    )
                    row = {
                        "file": fi.file,
                        "row_group": rg.index,
                        "scope": "page",
                        "page": page.index,
                        "column_name": col.name,
                        "verdict": verdict,
                        "trusted": verdict == OK,
                        "failure": None if verdict == OK else failure,
                        "detail": detail,
                    }
                    verdicts.append(row)
                    counters[verdict] = counters.get(verdict, 0) + 1

                # ---- 行组级: 声明 vs 事实 ----
                claimed_rg = ds.claimed_row_group_stats(
                    fi.file, rg.index, col.name
                )
                rg_verdict, rg_failure, rg_detail = _judge_row_group(
                    ds=ds,
                    file_name=fi.file,
                    rg_index=rg.index,
                    col=col,
                    actual_rg=actual_rg,
                    claimed_rg=claimed_rg,
                    actual_pages=actual_pages[col.name],
                    claimed_pages=claimed_pages[col.name],
                    collector=collector,
                    mask_sensitive=mask_sensitive,
                )
                verdicts.append({
                    "file": fi.file,
                    "row_group": rg.index,
                    "scope": "row_group",
                    "page": None,
                    "column_name": col.name,
                    "verdict": rg_verdict,
                    "trusted": rg_verdict == OK,
                    "failure": None if rg_verdict == OK else rg_failure,
                    "detail": rg_detail,
                })
                counters[rg_verdict] = counters.get(rg_verdict, 0) + 1

    trusted = sum(1 for v in verdicts if v["trusted"])
    summary = {
        "total_columns_scopes": len(verdicts),
        "trusted": trusted,
        "untrusted": len(verdicts) - trusted,
        "by_verdict": dict(sorted(counters.items())),
        "all_trusted": trusted == len(verdicts),
    }
    return {
        "run_id": run_id,
        "request_id": request_id,
        "dataset": ds.name,
        "started_at": started,
        "finished_at": _utcnow(),
        "summary": summary,
        "verdicts": verdicts,
        "diagnostics": [e.to_dict() for e in collector.events],
    }


def _safe_stats(st: ColumnStats | None, col: adapter.ColumnInfo,
                mask_sensitive: bool) -> dict[str, Any] | None:
    if st is None:
        return None
    data = st.to_json()
    if mask_sensitive and col.sensitive:
        return masking.mask_stats_dict(data, sensitive=True)
    return data


def _judge_page(
    *,
    actual: ColumnStats,
    claimed: ColumnStats | None,
    logical: LogicalType,
) -> tuple[str, str | None, dict[str, Any]]:
    if claimed is None:
        return STATS_MISSING, STATS_MISSING, {
            "reason": "无声明页级统计",
            "actual": {"count": actual.count, "null_count": actual.null_count},
        }
    failures = classify_diffs(actual, claimed)
    if not failures:
        return OK, None, {"reason": "声明统计与重算事实一致"}
    _, diffs = stats_equal(actual, claimed)
    return failures[0], failures[0], {"diffs": diffs, "all_failures": failures}


def _judge_row_group(
    *,
    ds: adapter.Dataset,
    file_name: str,
    rg_index: int,
    col: adapter.ColumnInfo,
    actual_rg: ColumnStats,
    claimed_rg: ColumnStats | None,
    actual_pages: list[ColumnStats],
    claimed_pages: list[ColumnStats | None],
    collector: EventCollector,
    mask_sensitive: bool,
) -> tuple[str, str | None, dict[str, Any]]:
    # 1) 无行组声明 -> missing
    if claimed_rg is None:
        ev = DiagnosticEvent(
            code=STATS_MISSING,
            decision=UNKNOWN,
            message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
            "无声明行组统计, 无法判定, 拒绝剪枝",
            file=file_name,
            row_group=rg_index,
            scope="row_group",
            column_name=col.name,
            state={
                "actual": _safe_stats(actual_rg, col, mask_sensitive),
            },
        )
        collector.add(ev)
        return STATS_MISSING, STATS_MISSING, {"reason": "无声明行组统计"}

    # 2) 声明 vs 重算事实
    failures = classify_diffs(actual_rg, claimed_rg)
    if failures:
        _, diffs = stats_equal(actual_rg, claimed_rg)
        verdict = failures[0]
        collector.add(DiagnosticEvent(
            code=verdict,
            decision=REJECT,
            message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
            f"声明行组统计 {verdict}, 拒绝该统计用于剪枝",
            file=file_name,
            row_group=rg_index,
            scope="row_group",
            column_name=col.name,
            state={
                "diffs": diffs,
                "all_failures": failures,
                "actual": _safe_stats(actual_rg, col, mask_sensitive),
                "claimed": _safe_stats(claimed_rg, col, mask_sensitive),
            },
        ))
        return verdict, verdict, {"diffs": diffs, "all_failures": failures}

    # 3) 聚合关系: 行组声明必须等于其页级声明的聚合 (页声明缺失则无法校验)
    if any(p is None for p in claimed_pages):
        missing = [i for i, p in enumerate(claimed_pages) if p is None]
        verdict = AGGREGATION_MISMATCH
        collector.add(DiagnosticEvent(
            code=verdict,
            decision=REJECT,
            message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
            f"页 {missing} 缺少声明, 页->行组聚合链不完整, 拒绝剪枝",
            file=file_name,
            row_group=rg_index,
            scope="row_group",
            column_name=col.name,
            state={"missing_pages": missing},
        ))
        return verdict, verdict, {"missing_pages": missing}

    try:
        agg = aggregate_stats(claimed_pages)
    except ValueError as exc:
        verdict = AGGREGATION_MISMATCH
        collector.add(DiagnosticEvent(
            code=verdict,
            decision=REJECT,
            message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
            f"页级聚合计数与行组声明矛盾 ({exc}), 拒绝剪枝",
            file=file_name,
            row_group=rg_index,
            scope="row_group",
            column_name=col.name,
            state={"error": str(exc)},
        ))
        return verdict, verdict, {"error": str(exc)}

    assert agg is not None
    _, agg_diffs = stats_equal(claimed_rg, agg)
    if agg_diffs:
        verdict = AGGREGATION_MISMATCH
        collector.add(DiagnosticEvent(
            code=verdict,
            decision=REJECT,
            message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
            "行组声明 != 页级声明聚合, 聚合关系不成立, 拒绝剪枝",
            file=file_name,
            row_group=rg_index,
            scope="row_group",
            column_name=col.name,
            state={
                "claimed_row_group": _safe_stats(claimed_rg, col, mask_sensitive),
                "aggregate_of_pages": _safe_stats(agg, col, mask_sensitive),
                "diffs": agg_diffs,
            },
        ))
        return verdict, verdict, {"diffs": agg_diffs}

    # 4) Parquet 内嵌统计作为第二独立来源交叉校验
    try:
        embedded = adapter.embedded_row_group_stats(
            ds, file_name, rg_index, col.name
        )
    except Exception as exc:  # pragma: no cover - 防御性
        embedded = None
        emb_note = f"内嵌统计读取异常: {exc}"
    else:
        emb_note = None

    if embedded is None:
        collector.add(DiagnosticEvent(
            code=EMBEDDED_INCONCLUSIVE,
            decision=UNKNOWN,
            message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
            "Parquet 内嵌统计缺失, 无第二来源; 但声明统计已与重算事实 "
            "及页级聚合一致, 接受",
            file=file_name,
            row_group=rg_index,
            scope="row_group",
            column_name=col.name,
            state={"note": emb_note or "no embedded stats"},
        ))
        # 无法获得第二来源不影响接受 (重算事实已是权威来源)
    else:
        conflict = _embedded_conflicts(embedded, claimed_rg, col.logical_type)
        if conflict:
            verdict = EMBEDDED_CONFLICT
            collector.add(DiagnosticEvent(
                code=verdict,
                decision=REJECT,
                message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
                "声明统计与 Parquet 内嵌统计冲突, 拒绝剪枝",
                file=file_name,
                row_group=rg_index,
                scope="row_group",
                column_name=col.name,
                state={"conflicts": conflict},
            ))
            return verdict, verdict, {"conflicts": conflict}

    collector.add(DiagnosticEvent(
        code=OK,
        decision=ACCEPT,
        message=f"{file_name} 行组 {rg_index} 列 {col.name}: "
        "声明统计与重算事实、页级聚合一致, 接受并允许剪枝",
        file=file_name,
        row_group=rg_index,
        scope="row_group",
        column_name=col.name,
        state={
            "claimed": _safe_stats(claimed_rg, col, mask_sensitive),
        },
    ))
    return OK, None, {"reason": "事实一致 + 聚合成立"}


def _embedded_conflicts(
    embedded: ColumnStats,
    claimed: ColumnStats,
    logical: LogicalType,
) -> list[str]:
    """交叉校验 Parquet 内嵌来源能表达的字段。"""
    conflicts: list[str] = []
    if embedded.count != claimed.count:
        conflicts.append(
            f"count: embedded={embedded.count} claimed={claimed.count}"
        )
    if embedded.null_count != claimed.null_count:
        conflicts.append(
            f"null_count: embedded={embedded.null_count} "
            f"claimed={claimed.null_count}"
        )
    if embedded.min is not None and claimed.min is not None:
        if not endpoint_equal(embedded.min, claimed.min, logical):
            # 有符号零差异: 内嵌来源不区分, 不算冲突
            signed_zero_only = (
                logical is LogicalType.FLOAT
                and embedded.min + 0.0 == claimed.min + 0.0 == 0.0
            )
            if not signed_zero_only:
                conflicts.append(
                    f"min: embedded={embedded.min!r} claimed={claimed.min!r}"
                )
    if embedded.max is not None and claimed.max is not None:
        if not endpoint_equal(embedded.max, claimed.max, logical):
            signed_zero_only = (
                logical is LogicalType.FLOAT
                and embedded.max + 0.0 == claimed.max + 0.0 == 0.0
            )
            if not signed_zero_only:
                conflicts.append(
                    f"max: embedded={embedded.max!r} claimed={claimed.max!r}"
                )
    return conflicts


def _emit_page_events(
    collector: EventCollector,
    ds: adapter.Dataset,
    file_name: str,
    rg_index: int,
    page_index: int,
    col: adapter.ColumnInfo,
    actual: ColumnStats,
    claimed: ColumnStats | None,
    verdict: str,
    failure: str | None,
    detail: dict[str, Any],
    mask_sensitive: bool,
) -> None:
    loc = (
        f"{file_name} 行组 {rg_index} 页 {page_index} 列 {col.name}"
    )
    state = {
        "actual": _safe_stats(actual, col, mask_sensitive),
        "claimed": _safe_stats(claimed, col, mask_sensitive),
    }
    if verdict == OK:
        collector.add(DiagnosticEvent(
            code=OK,
            decision=ACCEPT,
            message=f"{loc}: 页级声明统计与重算事实一致, 接受",
            file=file_name,
            row_group=rg_index,
            scope="page",
            page=page_index,
            column_name=col.name,
            state=state,
        ))
    elif verdict == STATS_MISSING:
        collector.add(DiagnosticEvent(
            code=STATS_MISSING,
            decision=UNKNOWN,
            message=f"{loc}: 无声明页级统计, 无法判定, 拒绝剪枝 (全扫兜底)",
            file=file_name,
            row_group=rg_index,
            scope="page",
            page=page_index,
            column_name=col.name,
            state=state,
        ))
    else:
        collector.add(DiagnosticEvent(
            code=verdict,
            decision=REJECT,
            message=f"{loc}: {verdict}, 拒绝该统计用于剪枝",
            file=file_name,
            row_group=rg_index,
            scope="page",
            page=page_index,
            column_name=col.name,
            state={**state, "detail": detail},
        ))
