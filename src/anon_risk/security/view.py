"""出站白名单视图：内核结果 -> 可安全离开进程的 JSON 结构。

强制约束（即便内核对象携带原始值，这里也不允许带出）：

- 等价类只暴露**计数**：类大小、非 NULL 不同敏感值数、敏感值频次分布、
  NULL 计数、判定原因代码；不暴露任何原始准标识符取值。
- 类的关联键为 HMAC 指纹（每运行独立密钥），不可逆且跨运行不同。
- 敏感值频次按“出现次数”聚合，输出时**只给出次数到类数的分布**，
  单类内具体敏感值文本一律不输出（否则小类中计数=1 的标签即泄露）。
"""

from __future__ import annotations

from ..kernel.equivalence import RiskReport
from ..kernel.optimizer import Suggestion


def class_view(cls, fingerprint: str) -> dict:
    # 敏感值 -> 次数 的分布本身也可能泄露文本：只保留“次数 -> 该次数出现几次”
    freq_hist: dict[int, int] = {}
    for n in cls.sensitive_value_counts.values():
        freq_hist[n] = freq_hist.get(n, 0) + 1
    return {
        "index": cls.index,
        "class_fingerprint": fingerprint,
        "size": cls.size,
        "prosecutor_risk": round(cls.prosecutor_risk, 10),
        "risk_category": cls.risk,
        "k_ok": cls.k_ok,
        "l_ok": cls.l_ok,
        "reasons": list(cls.reasons),
        "qi_key_contains_null": cls.qi_has_null,
        "sensitive_distinct_non_null": cls.sensitive_distinct,
        "sensitive_null_members": cls.sensitive_nulls,
        "sensitive_frequency_histogram": dict(sorted(freq_hist.items())),
    }


def report_view(report: RiskReport, hmac) -> dict:
    return {
        "row_count": report.row_count,
        "qi_columns": report.qi_columns,
        "sensitive_columns": report.sensitive_columns,
        "levels": report.levels,
        "k": report.k,
        "l": report.l,
        "k_anonymized": report.k_ok,
        "l_diverse": report.l_ok,
        "risk_overall": _overall(report),
        "worst_prosecutor_risk": round(report.worst_prosecutor_risk, 10),
        "average_prosecutor_risk": round(report.average_prosecutor_risk, 10),
        "rows_in_violating_classes": report.rows_in_violating_classes,
        "class_size_distribution": {str(k): v for k, v in
                                    report.size_distribution.items()},
        "null_qi_rows": report.null_q_rows,
        "sensitive_null_members_total": report.sensitive_nulls_total,
        "null_counts_by_column": report.null_counts,
        "loss_metric": report.loss_metric,
        "discernibility": report.discernibility,
        "metric_version": report.metric_version,
        "disclaimer": report.disclaimer,
        "classes": [
            class_view(c, hmac.fingerprint([str(x) if x is not None else "\\x00NULL"
                                            for x in c.qi_key]))
            for c in report.classes
        ],
    }


def _overall(report: RiskReport) -> str:
    if not (report.k_ok and report.l_ok):
        return "HIGH"
    # 与内核一致：任一 MEDIUM 即 MEDIUM，所有类均 LOW 才 LOW
    if any(c.risk == "MEDIUM" for c in report.classes):
        return "MEDIUM"
    return "LOW"


def suggestion_view(s: Suggestion) -> dict:
    out: dict = {
        "status": s.status,
        "feasible": s.feasible,
        "k": s.k,
        "l": s.l,
        "evaluated_vectors": s.evaluated_vectors,
        "total_vectors": s.total_vectors,
        "verdict_basis": s.verdict_basis,
        "metric_disclaimer": (
            "建议只最小化给定泛化格点内的信息损失指标（DM，平局看 LM），"
            "不构成完整隐私保证。"
        ),
    }
    if s.feasible:
        out.update({
            "levels": s.levels,
            "levels_tuple": s.levels_tuple,
            "class_count": s.class_count,
            "class_sizes": s.class_sizes,
            "discernibility": s.discernibility,
            "loss_metric": s.loss_metric,
            "k_anonymized": True,
            "l_diverse": True,
        })
    else:
        out.update({
            "class_count": s.class_count,
            "class_sizes": s.class_sizes,
            "discernibility": s.discernibility,
            "loss_metric": s.loss_metric,
            "unreachable_evidence": s.unreachable_evidence,
        })
    return out
