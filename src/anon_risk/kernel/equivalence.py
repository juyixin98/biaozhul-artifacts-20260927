"""等价类分组与风险指标（真实计数，不做均匀假设）。

指标
----
- k-匿名：每个等价类行数 >= k。检察官重识别风险 = 1/类大小。
- l-多样性（distinct，保守口径）：每类内**非 NULL**的不同敏感值数 >= l；
  敏感值为 NULL 的成员单独计数，绝不把 NULL 当作一种真实敏感属性凑数。
- 信息损失 LM：列级深度归一化后按类大小加权平均（保留真实类大小）。
- 可辨识代价 DM = sum(类大小^2)：泛化建议的主优化目标，直接依赖真实计数。

风险类别（按类给出，仅聚合数据；原始 QI/敏感值永不出现）::

    HIGH   未达 k 或未达 l（存在重识别/属性泄露风险）
    MEDIUM 已达标但类大小 < factor*k（贴近阈值）
    LOW    已达标且类大小 >= factor*k
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ..errors import ErrorCode, RiskError
from ..logging_setup import get_logger
from .types import Dataset, NULL

log = get_logger("equivalence")

DISCLAIMER = (
    "k-匿名与 l-多样性只刻画等价类规模与敏感属性多样性，"
    "不构成完整隐私保证：它们不防御背景知识攻击、近似/相似性攻击、"
    "差分攻击，也不覆盖外部链接或推断攻击。结果须与其他控制措施一起使用。"
)


@dataclass
class EquivalenceClass:
    index: int                      # 类序号（报告里的稳定标识）
    size: int
    qi_key: tuple[Optional[str], ...]   # 仅内核内可见；出站前替换为指纹
    sensitive_distinct: int         # 非 NULL 不同敏感值数
    sensitive_nulls: int            # 敏感值为 NULL 的成员数
    sensitive_value_counts: dict[str, int]
    # 标记该类的 QI 键中是否含 NULL（这些行没有被移除）
    qi_has_null: bool
    k_ok: bool
    l_ok: bool
    risk: str                       # HIGH / MEDIUM / LOW
    prosecutor_risk: float          # 1/size
    reasons: list[str] = field(default_factory=list)


@dataclass
class RiskReport:
    row_count: int
    qi_columns: list[str]
    sensitive_columns: list[str]
    levels: dict[str, int]
    k: int
    l: int
    k_ok: bool
    l_ok: bool
    worst_prosecutor_risk: float
    average_prosecutor_risk: float
    classes: list[EquivalenceClass]
    rows_in_violating_classes: int
    # 各类大小到行数的分布（聚合证据，便于独立复核）
    size_distribution: dict[int, int]
    null_q_rows: int                # QI 含 NULL 的行数（保留在样本中）
    sensitive_nulls_total: int
    loss_metric: float
    discernibility: int
    metric_version: str
    disclaimer: str
    # 解析证据
    null_counts: dict[str, int]


def group_classes(rows: list[dict], qi_columns: list[str],
                  sensitive_column: str,
                  qi_keys: list[tuple[Optional[str], ...]]) -> list[EquivalenceClass]:
    """按泛化后的 QI 键分组并统计真实类大小与敏感值分布。"""
    buckets: dict[tuple[Optional[str], ...], list[int]] = defaultdict(list)
    for i, key in enumerate(qi_keys):
        buckets[key].append(i)

    classes: list[EquivalenceClass] = []
    def _sort_key(item):
        # 键里可能含 NULL；None 与 str 不可直接比较，用类型标记稳定排序
        key = item[0]
        return (-len(item[1]), tuple((0, "") if p is NULL else (1, p)
                                     for p in key))

    for idx, (key, member_idx) in enumerate(sorted(buckets.items(), key=_sort_key)):
        counts: dict[str, int] = defaultdict(int)
        nulls = 0
        for mi in member_idx:
            val = rows[mi][sensitive_column]
            if val is NULL:
                nulls += 1
            else:
                counts[val] += 1
        classes.append(EquivalenceClass(
            index=idx,
            size=len(member_idx),
            qi_key=key,
            sensitive_distinct=len(counts),
            sensitive_nulls=nulls,
            sensitive_value_counts=dict(counts),
            qi_has_null=any(p is NULL for p in key),
            k_ok=False, l_ok=False, risk="HIGH",
            prosecutor_risk=1.0 / len(member_idx),
        ))
    return classes


def evaluate(
    dataset: Dataset,
    qi_keys: list[tuple[Optional[str], ...]],
    levels: dict[str, int],
    k: int,
    l: int,
    *,
    risk_medium_factor: int = 2,
    metric_version: str = "1.0.0",
) -> RiskReport:
    """计算完整风险报告。``sensitive`` 取第一个敏感列（多敏感列时逐列另算）。"""
    if k < 1 or l < 1:
        raise RiskError("k 与 l 必须为正整数",
                        code=ErrorCode.INVALID_PARAMETER,
                        details={"k": k, "l": l})

    sensitive_column = dataset.sensitive_columns[0]
    classes = group_classes(dataset.rows, dataset.qi_columns,
                            sensitive_column, qi_keys)
    depths = {c: dataset.hierarchies[c].depth for c in dataset.qi_columns}

    violating_rows = 0
    size_dist: dict[int, int] = defaultdict(int)
    null_q_rows = 0
    sensitive_nulls_total = 0
    weighted_depth = 0.0
    discernibility = 0
    risk_sum = 0.0
    worst = 0.0
    k_ok_all = True
    l_ok_all = True

    for cls in classes:
        cls.k_ok = cls.size >= k
        cls.l_ok = cls.sensitive_distinct >= l
        if not cls.k_ok:
            cls.reasons.append(f"class_size_{cls.size}_lt_k_{k}")
        if not cls.l_ok:
            cls.reasons.append(
                f"distinct_non_null_{cls.sensitive_distinct}_lt_l_{l}"
            )
        if cls.qi_has_null:
            cls.reasons.append("qi_key_contains_null")
            null_q_rows += cls.size

        if cls.k_ok and cls.l_ok:
            cls.risk = "LOW" if cls.size >= risk_medium_factor * k else "MEDIUM"
        else:
            cls.risk = "HIGH"
            violating_rows += cls.size

        size_dist[cls.size] += cls.size
        sensitive_nulls_total += cls.sensitive_nulls
        weighted_depth += cls.size * sum(
            levels[c] / depths[c] if depths[c] > 0 else 0.0
            for c in dataset.qi_columns
        ) / len(dataset.qi_columns)
        discernibility += cls.size * cls.size
        risk_sum += cls.prosecutor_risk * cls.size
        worst = max(worst, cls.prosecutor_risk)

    n = len(dataset.rows)
    lm = weighted_depth / n if n else 0.0
    k_ok_all = all(c.k_ok for c in classes)
    l_ok_all = all(c.l_ok for c in classes)

    report = RiskReport(
        row_count=n,
        qi_columns=list(dataset.qi_columns),
        sensitive_columns=list(dataset.sensitive_columns),
        levels=dict(levels),
        k=k, l=l,
        k_ok=k_ok_all,
        l_ok=l_ok_all,
        worst_prosecutor_risk=worst,
        average_prosecutor_risk=risk_sum / n if n else 0.0,
        classes=classes,
        rows_in_violating_classes=violating_rows,
        size_distribution=dict(sorted(size_dist.items())),
        null_q_rows=null_q_rows,
        sensitive_nulls_total=sensitive_nulls_total,
        loss_metric=round(lm, 10),
        discernibility=discernibility,
        metric_version=metric_version,
        disclaimer=DISCLAIMER,
        null_counts=dict(dataset.null_counts),
    )
    log.info(
        "等价类评估完成",
        extra={"event": {
            "n": n, "k": k, "l": l,
            "classes": len(classes),
            "k_ok": k_ok_all, "l_ok": l_ok_all,
            "worst_prosecutor_risk": worst,
            "dm": discernibility, "loss_metric": report.loss_metric,
            "violating_rows": violating_rows,
        }},
    )
    return report
