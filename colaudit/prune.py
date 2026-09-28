"""基于受信页级统计的剪枝判定 (验收规则 4: 坏统计不得继续用于剪枝)。

进入本模块的前提是调用方只传入 verdict == "ok" 的统计; 任何缺失、
错误、聚合不成立的页都应在调用前被排除, 走全表扫描。

截断与可信区间 (验收规则 2):
* min_truncated=True 时, 声明 min 只是真实 min 的前缀, 真实 min >= 声明 min,
  因此"下界"仍有效, 但只有下界语义;
* max_truncated=True 时, 声明 max 只是真实 max 的前缀, 真实 max <= 声明 max
  不成立 (前缀可能更小), 上界失效;
* 谓词只可使用与截断标志相容的那一侧边界。

NaN: 六个比较谓词不匹配 NaN; 若页内含 NaN, 我们仍可按非 NaN 区间剪枝
(NaN 行对比较谓词本就不命中)。IS NULL 只命中 NULL; IS NOT NULL 永远扫描。
"""
from __future__ import annotations

from dataclasses import dataclass

from .logical import is_nan, sort_key
from .stats import ColumnStats

SCAN = "scan"
SKIP = "skip"
PRUNE_DECISIONS = frozenset({SCAN, SKIP})

OP_IS_NULL = "is_null"
OP_NOT_NULL = "not_null"
COMPARE_OPS = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})


@dataclass
class Predicate:
    column: str
    op: str
    value: Any = None

    def __post_init__(self) -> None:
        if self.op not in COMPARE_OPS and self.op not in {
            OP_IS_NULL,
            OP_NOT_NULL,
        }:
            raise ValueError(f"不支持的谓词: {self.op}")


@dataclass
class PageDecision:
    file: str
    row_group: int
    page: int
    decision: str
    reason: str

    @property
    def skipped(self) -> bool:
        return self.decision == SKIP


def decide_page(
    pred: Predicate,
    stats: ColumnStats,
    *,
    file: str,
    row_group: int,
    page: int,
) -> PageDecision:
    """对单个"受信"页统计做剪枝判定。

    注意: 调用方必须保证 stats 已通过审计 (trusted)。本函数不重新校验,
    但对统计自身不足以决策的情形 (全 NULL、边界缺失) 一律保守扫描。
    """
    loc = {"file": file, "row_group": row_group, "page": page}

    if pred.op == OP_IS_NULL:
        if stats.null_count == 0:
            return PageDecision(**loc, decision=SKIP,
                                reason="trusted: null_count=0")
        return PageDecision(**loc, decision=SCAN,
                            reason="trusted: 页内含 NULL")

    if pred.op == OP_NOT_NULL:
        # is-not-null 只在全 NULL 时可跳过
        if stats.count > 0 and stats.null_count == stats.count:
            return PageDecision(**loc, decision=SKIP,
                                reason="trusted: 整页全 NULL")
        return PageDecision(**loc, decision=SCAN,
                            reason="trusted: not_null 需扫描")

    # 比较谓词: 目标值类型
    target = pred.value
    if is_nan(target):
        # value = NaN: 比较谓词不可能命中任何行, 但这是查询计划层的重写;
        # 页级保守起见仍扫描 (审计只信任统计, 不重写谓词)。
        return PageDecision(**loc, decision=SCAN,
                            reason="目标为 NaN, 比较谓词不命中, 交由执行层")

    # 全 NULL / 全 NaN: 比较谓词无命中
    if stats.comparable_count == 0:
        return PageDecision(
            **loc,
            decision=SKIP,
            reason=(
                "trusted: 页内无可比较值 "
                f"(null={stats.null_count}, nan={stats.nan_count})"
            ),
        )

    lo = stats.min
    hi = stats.max
    if lo is None or hi is None:
        return PageDecision(**loc, decision=SCAN,
                            reason="trusted: 边界缺失, 保守扫描")

    op = pred.op
    if op == "lt":  # value < target; 若最小可比值 >= target -> 跳过
        if sort_key(lo, stats.logical_type) >= sort_key(target, stats.logical_type):
            return PageDecision(**loc, decision=SKIP,
                                reason="trusted: min >= target, 无 lt 命中")
        return PageDecision(**loc, decision=SCAN, reason="区间与谓词重叠")

    if op == "le":  # value <= target; 若最小可比值 > target -> 跳过
        if sort_key(lo, stats.logical_type) > sort_key(target, stats.logical_type):
            return PageDecision(**loc, decision=SKIP,
                                reason="trusted: min > target, 无 le 命中")
        return PageDecision(**loc, decision=SCAN, reason="区间与谓词重叠")

    if op == "gt":  # value > target; 上界必须可信
        if stats.max_truncated:
            return PageDecision(
                **loc, decision=SCAN,
                reason="max 被截断, 上界不可信, 禁用剪枝 (规则 2/4)")
        if sort_key(hi, stats.logical_type) <= sort_key(target, stats.logical_type):
            return PageDecision(**loc, decision=SKIP,
                                reason="trusted: max <= target, 无 gt 命中")
        return PageDecision(**loc, decision=SCAN, reason="区间与谓词重叠")

    if op == "ge":  # value >= target; 上界必须可信
        if stats.max_truncated:
            return PageDecision(
                **loc, decision=SCAN,
                reason="max 被截断, 上界不可信, 禁用剪枝 (规则 2/4)")
        if sort_key(hi, stats.logical_type) < sort_key(target, stats.logical_type):
            return PageDecision(**loc, decision=SKIP,
                                reason="trusted: max < target, 无 ge 命中")
        return PageDecision(**loc, decision=SCAN, reason="区间与谓词重叠")

    if op == "eq":
        if stats.max_truncated and sort_key(target, stats.logical_type) >= sort_key(
            hi, stats.logical_type
        ):
            # 上界不可信时, 目标在声明上界右侧无法排除
            return PageDecision(
                **loc, decision=SCAN,
                reason="max 被截断且 target >= 声明 max, 禁用剪枝")
        if (
            sort_key(target, stats.logical_type) < sort_key(lo, stats.logical_type)
            or sort_key(target, stats.logical_type) > sort_key(
                hi, stats.logical_type)
        ):
            return PageDecision(**loc, decision=SKIP,
                                reason="trusted: target 不在 [min,max]")
        return PageDecision(**loc, decision=SCAN, reason="区间包含 target")

    if op == "ne":
        # 只有"整页非 NULL 可比值全部等于 target 且无 NULL/NaN"才能跳过
        # (NULL -> UNKNOWN, NaN -> UNKNOWN, 都不算 ne 命中, 可安全跳过)
        if (
            not stats.max_truncated
            and endpoint_all_equal(stats, target)
        ):
            return PageDecision(
                **loc, decision=SKIP,
                reason="trusted: 整页值 = target (含 NULL/NaN 不命中 ne)")
        return PageDecision(**loc, decision=SCAN, reason="ne 需扫描")

    return PageDecision(**loc, decision=SCAN, reason="未知谓词, 保守扫描")


def endpoint_all_equal(stats: ColumnStats, target: Any) -> bool:
    if stats.min is None or stats.max is None:
        return False
    lt = stats.logical_type
    from .logical import endpoint_equal

    return (
        endpoint_equal(stats.min, target, lt)
        and endpoint_equal(stats.max, target, lt)
    )
