"""泛化建议：在完整格点上穷举，选真实计数下信息损失最小的可行向量。

为什么穷举
----------
本项目定位为小合成表的可验证后端。对小表穷举能给出**可证明最优**建议，
测试可用独立暴力参考实现逐一核对；格点数 = prod(depth_i+1)，由配置
``lattice_combo_cap`` 硬性保护，超过则报 ``LATTICE_TOO_LARGE``，绝不退化成
未经验证的近似结果。

目标（保持真实等价类计数，不做均匀假设）

1. 可行性：所有类同时满足 k 与 l（含 NULL 的保守口径）。
2. 主目标 DM = sum(class_size^2)，可辨识代价最小；DM 由真实类大小决定。
3. 同 DM 时选深度归一化损失 LM 最小；再同则按列序取字典序更小的向量，
   保证结果确定、可复测。

快速不可达判定：全泛化（每列取最大深度）仍不满足 k/l 时，阈值不可达。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

from ..errors import ErrorCode, RiskError
from ..logging_setup import get_logger
from . import equivalence as eq
from .hierarchy import MaterializedHierarchy, apply_vector
from .types import Dataset, NULL

log = get_logger("optimizer")


@dataclass
class Suggestion:
    status: str                         # "FEASIBLE" | "UNREACHABLE"
    feasible: bool
    k: int
    l: int
    levels: dict[str, int]
    levels_tuple: list[int]
    class_count: int
    class_sizes: list[int]
    discernibility: int
    loss_metric: float
    evaluated_vectors: int
    total_vectors: int
    verdict_basis: list[str] = field(default_factory=list)
    best_k_ok: Optional[bool] = None
    best_l_ok: Optional[bool] = None
    # 不可达时的证据（聚合）
    unreachable_evidence: Optional[dict] = None


def _all_vectors(depths: list[int]):
    yield from itertools.product(*(range(d + 1) for d in depths))


def suggest(
    dataset: Dataset,
    materialized: dict[str, MaterializedHierarchy],
    k: int,
    l: int,
    *,
    combo_cap: int = 10_000,
    risk_medium_factor: int = 2,
    metric_version: str = "1.0.0",
) -> Suggestion:
    qi = dataset.qi_columns
    depths = [dataset.hierarchies[c].depth for c in qi]
    total = 1
    for d in depths:
        total *= (d + 1)
    if total > combo_cap:
        raise RiskError(
            f"泛化格点组合数 {total} 超过上限 {combo_cap}，穷举被拒绝",
            code=ErrorCode.LATTICE_TOO_LARGE,
            details={"total_vectors": total, "cap": combo_cap,
                     "qi_depths": dict(zip(qi, depths))},
        )

    # 先评估全泛化向量：它给出最粗划分，仍不可行 => 阈值本身不可达
    max_vec = tuple(depths)
    max_keys = _keys_at(dataset, materialized, qi, max_vec)
    max_report = eq.evaluate(
        dataset, max_keys, dict(zip(qi, max_vec)), k, l,
        risk_medium_factor=risk_medium_factor, metric_version=metric_version,
    )
    distinct_sensitive_non_null = len({
        r[dataset.sensitive_columns[0]]
        for r in dataset.rows
        if r[dataset.sensitive_columns[0]] is not NULL
    })

    if not (max_report.k_ok and max_report.l_ok):
        reasons = []
        if not max_report.k_ok:
            reasons.append(
                f"full_generalization_min_class_{min(c.size for c in max_report.classes)}_lt_k_{k}"
            )
        if not max_report.l_ok:
            reasons.append(
                f"distinct_non_null_sensitive_{distinct_sensitive_non_null}_lt_l_{l}"
            )
        log.warning(
            "阈值不可达：全泛化仍不满足",
            extra={"event": {"k": k, "l": l, "reasons": reasons,
                             "evaluated_vectors": 1, "total_vectors": total}},
        )
        return Suggestion(
            status="UNREACHABLE", feasible=False, k=k, l=l,
            levels={}, levels_tuple=[], class_count=len(max_report.classes),
            class_sizes=sorted((c.size for c in max_report.classes), reverse=True),
            discernibility=max_report.discernibility,
            loss_metric=max_report.loss_metric,
            evaluated_vectors=1, total_vectors=total,
            verdict_basis=reasons,
            unreachable_evidence={
                "full_generalization_k_ok": max_report.k_ok,
                "full_generalization_l_ok": max_report.l_ok,
                "distinct_non_null_sensitive_values": distinct_sensitive_non_null,
                "row_count": len(dataset.rows),
                "violating_classes": [
                    {"size": c.size,
                     "distinct_non_null_sensitive": c.sensitive_distinct,
                     "k_ok": c.k_ok, "l_ok": c.l_ok}
                    for c in max_report.classes if not (c.k_ok and c.l_ok)
                ],
            },
        )

    # 可行：按总深度分组、组内按列序字典序评估；记录最优（DM, LM, 向量序）
    best: Optional[tuple] = None  # (dm, lm, vec, report, keys)
    best_report: Optional[eq.RiskReport] = None
    evaluated = 0
    progress_step = max(1, total // 10)

    for vec in itertools.product(*(range(d + 1) for d in depths)):
        evaluated += 1
        keys = _keys_at(dataset, materialized, qi, vec)
        report = eq.evaluate(
            dataset, keys, dict(zip(qi, vec)), k, l,
            risk_medium_factor=risk_medium_factor, metric_version=metric_version,
        )
        if report.k_ok and report.l_ok:
            candidate = (report.discernibility, report.loss_metric, vec, report)
            if best is None or candidate[:3] < (best[0], best[1], best[2]):
                best = candidate
                best_report = report
        if evaluated == 1 or evaluated % progress_step == 0 or evaluated == total:
            log.info(
                "穷举进度",
                extra={"event": {
                    "progress": f"{evaluated}/{total}",
                    "vector": list(vec),
                    "feasible": report.k_ok and report.l_ok,
                    "vector_dm": report.discernibility,
                    "best_dm": best[0] if best else None,
                }},
            )

    assert best is not None and best_report is not None  # 全泛化可行 => 必有解
    vec = best[2]
    basis = [
        f"exhaustive_enumeration_of_{total}_vectors",
        f"min_discernibility_dm={best[0]}",
        f"tie_break_loss_metric={best[1]}",
        "counts_are_real_class_sizes",
    ]
    log.info(
        "最优泛化建议确定",
        extra={"event": {
            "levels": dict(zip(qi, vec)),
            "dm": best[0], "lm": best[1],
            "class_sizes": sorted((c.size for c in best_report.classes), reverse=True),
            "evaluated": evaluated, "total": total,
            "basis": basis,
        }},
    )
    return Suggestion(
        status="FEASIBLE", feasible=True, k=k, l=l,
        levels=dict(zip(qi, vec)),
        levels_tuple=list(vec),
        class_count=len(best_report.classes),
        class_sizes=sorted((c.size for c in best_report.classes), reverse=True),
        discernibility=best[0],
        loss_metric=best[1],
        evaluated_vectors=evaluated,
        total_vectors=total,
        verdict_basis=basis,
        best_k_ok=True,
        best_l_ok=True,
    )


def _keys_at(dataset: Dataset, materialized: dict[str, MaterializedHierarchy],
             qi: list[str], vec: tuple[int, ...]):
    return apply_vector(
        dataset.rows, qi, materialized, dict(zip(qi, vec))
    )
