"""独立测试参考实现（test oracle）。

严格要求：本模块**只使用 Python 标准库**，且**不导入 anon_risk 包**。
它独立地对合成夹具做三件事：

1. 用独立的字符串/数值泛化规则重新计算等价类；
2. 对小表暴力枚举全部泛化向量，独立选出最优 (DM, LM)；
3. 给出每一步的判定依据，供测试与被测实现交叉核对。

夹具的期望数值（最优向量、类大小、DM）另在 ``expected.py`` 中手工给出，
因此参考答案不是由被测核心“自己生成”的：测试断言的是手工答案，同时用
这份独立代码做全量交叉计算。
"""

from __future__ import annotations

import csv
import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ---------- 独立泛化规则（与生产实现平行编写，刻意不复用其代码） ----------

def prefix_value(v: Optional[str], keep: int) -> Optional[str]:
    if v is None:
        return None
    assert len(v) >= keep, f"值 {v!r} 短于 keep={keep}"
    return v[:keep]


def range_label(v: Optional[str], bins: list[float]) -> Optional[str]:
    if v is None:
        return None
    x = float(v)
    if x == bins[-1]:
        return f"[{bins[-2]},{bins[-1]}]"
    for lo, hi in zip(bins[:-1], bins[1:]):
        if lo <= x < hi:
            return f"[{lo},{hi}]"
    raise AssertionError(f"值 {x} 超出 bins {bins}")


def map_label(v: Optional[str], mapping: dict[str, str]) -> Optional[str]:
    if v is None:
        return None
    return mapping[v]


# ---------- tiny 夹具的手工层级表（测试自有，独立于 JSON 夹具的生产解析） ---
# 层级 0..3 / 0..2；None=NULL 保持不变
TINY_ZIP_PARTITIONS = {
    0: {"10001": "10001", "10002": "10002", "10003": "10003",
        "12001": "12001", "12002": "12002", "12003": "12003"},
    1: {"10001": "1000", "10002": "1000", "10003": "1000",
        "12001": "1200", "12002": "1200", "12003": "1200"},
    2: {"10001": "10", "10002": "10", "10003": "10",
        "12001": "12", "12002": "12", "12003": "12"},
    3: {"10001": "1x", "10002": "1x", "10003": "1x",
        "12001": "1x", "12002": "1x", "12003": "1x"},
}

TINY_AGE_PARTITIONS = {
    0: {"23": "23", "25": "25", "31": "31", "35": "35", "40": "40", "42": "42"},
    1: {"23": "[0,30]", "25": "[0,30]",
        "31": "[30,50]", "35": "[30,50]", "40": "[30,50]", "42": "[30,50]"},
    2: {"23": "[0,120]", "25": "[0,120]", "31": "[0,120]",
        "35": "[0,120]", "40": "[0,120]", "42": "[0,120]"},
}


@dataclass
class OracleRow:
    zip: Optional[str]
    age: Optional[str]
    disease: Optional[str]


def load_tiny_rows() -> list[OracleRow]:
    """直接从 CSV 读取（与生产 JSON 夹具相互独立），空单元格 -> None。"""
    rows: list[OracleRow] = []
    with open(FIXTURES / "tiny.csv", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(OracleRow(
                zip=(r["zip"].strip() or None),
                age=(r["age"].strip() or None),
                disease=(r["disease"].strip() or None),
            ))
    return rows


def label_for(column: str, level: int, raw: Optional[str]) -> Optional[str]:
    table = TINY_ZIP_PARTITIONS if column == "zip" else TINY_AGE_PARTITIONS
    return table[level][raw]


def class_partition(rows: list[OracleRow], zlevel: int,
                    alevel: int) -> dict[tuple, list[OracleRow]]:
    parts: dict[tuple, list[OracleRow]] = {}
    for r in rows:
        key = (label_for("zip", zlevel, r.zip),
               label_for("age", alevel, r.age))
        parts.setdefault(key, []).append(r)
    return parts


def k_ok(parts: dict[tuple, list[OracleRow]], k: int) -> bool:
    return all(len(g) >= k for g in parts.values())


def l_ok(parts: dict[tuple, list[OracleRow]], l: int) -> bool:
    """保守口径：非 NULL 的不同敏感值数 >= l；NULL 不凑数。"""
    return all(
        len({r.disease for r in g if r.disease is not None}) >= l
        for g in parts.values()
    )


def discernibility(parts: dict[tuple, list[OracleRow]]) -> int:
    return sum(len(g) ** 2 for g in parts.values())


def loss_metric(parts: dict[tuple, list[OracleRow]], zlevel: int,
                alevel: int, n: int) -> float:
    # 列深度：zip=3, age=2；按类大小加权的平均归一化深度
    depth_sum = sum(len(g) * (zlevel / 3 + alevel / 2) / 2 for g in parts.values())
    return round(depth_sum / n, 10)


def brute_force_optimum(rows: list[OracleRow], k: int, l: int):
    """枚举 4*3=12 个向量，返回 (feasible, best_vec, dm, lm, feasible_vectors)。"""
    feasible = []
    for zv, av in itertools.product(range(4), range(3)):
        parts = class_partition(rows, zv, av)
        if k_ok(parts, k) and l_ok(parts, l):
            feasible.append((zv, av, discernibility(parts),
                             loss_metric(parts, zv, av, len(rows)),
                             sorted((len(g) for g in parts.values()), reverse=True)))
    if not feasible:
        return False, None, None, None, []
    # 与生产相同的确定性次序：DM 升序、LM 升序、向量字典序
    feasible.sort(key=lambda t: (t[2], t[3], (t[0], t[1])))
    best = feasible[0]
    return True, (best[0], best[1]), best[2], best[3], feasible


def load_tiny_payload() -> dict:
    """读取生产格式的 JSON 夹具（供测试驱动 API；仅用于输入，不用于期望）。"""
    return json.loads((FIXTURES / "tiny.json").read_text(encoding="utf-8"))
