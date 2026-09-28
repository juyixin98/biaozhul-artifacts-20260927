"""独立参考预言机（reference oracle）。

这个模块**不导入被测内核** ``app.core.anonymization``，而是用最朴素、
显然正确的方式重新实现一遍：

* :func:`naive_generalize` —— 直接按层级字典查表泛化；
* :func:`naive_classes`    —— ``dict`` 手工分组计数；
* :func:`naive_loss`       —— 独立推导的组扩张损失；
* :func:`brute_force_optimum` —— 不带任何剪枝地枚举**全部**层级组合。

测试用它交叉核验被测实现的最优层级、信息损失、等价类规模分布；
同时断言被测实现探索的叶子数 ≤ 全空间（剪枝不改变答案的前提），
并对小表断言两者逐组合一致。

参考答案不来自被测核心自身，满足"答案不能由被测实现生成"的要求。
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

NULL = "\x00__NULL__"


def canon(v: Any) -> str:
    if v is None:
        return NULL
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    return NULL if s == "" else s


def levels_for(fixture_columns: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for c in fixture_columns:
        if c.get("role") == "quasi_identifier":
            levels = []
            for lev in c["hierarchy"]["levels"]:
                m = {canon(k): canon(v) for k, v in lev.items()}
                m.setdefault(NULL, NULL)
                levels.append(m)
            out[c["name"]] = levels
    return out


def normalize_rows(fixture: dict[str, Any]) -> list[dict[str, str]]:
    cols = [c["name"] for c in fixture["columns"]]
    out = []
    for r in fixture["rows"]:
        out.append({c: canon(r.get(c)) for c in cols})
    return out


def qi_names(fixture: dict[str, Any]) -> list[str]:
    return [c["name"] for c in fixture["columns"] if c["role"] == "quasi_identifier"]


def sens_names(fixture: dict[str, Any]) -> list[str]:
    return [c["name"] for c in fixture["columns"] if c["role"] == "sensitive"]


def naive_generalize(value: str, mapping: dict[str, str]) -> str:
    # NULL 恒等传播；其余严格查表（缺失即键错误，独立实现选择 fail-fast）
    if value == NULL:
        return NULL
    return mapping[value]


def naive_classes(
    rows: list[dict[str, str]],
    qi: list[str],
    sens: list[str],
    levels_map: dict[str, list[dict[str, str]]],
    chosen: dict[str, int],
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        key = tuple(naive_generalize(r[c], levels_map[c][chosen[c]]) for c in qi)
        buckets[key].append(i)
    classes = []
    for members in buckets.values():
        sigs = Counter(tuple(rows[i][s] for s in sens) for i in members)
        classes.append(
            {
                "size": len(members),
                "distinct_sensitive": len(sigs),
                "max_sensitive_frequency": max(sigs.values()),
                "contains_null_qi": any(
                    any(rows[i][c] == NULL for c in qi) for i in members
                ),
            }
        )
    classes.sort(key=lambda d: (-d["size"], -d["distinct_sensitive"]))
    return classes


def naive_loss(
    rows: list[dict[str, str]],
    qi: list[str],
    levels_map: dict[str, list[dict[str, str]]],
    chosen: dict[str, int],
) -> float:
    """逐行逐 QI 的平均组扩张损失；NULL（缺失）贡献 0。

    组大小按**样本内实际值**计（与被测实现一致）：层级声明的超集域里
    未在样本出现的值不计入组扩张。
    """
    total = 0.0
    for c in qi:
        domain = {r[c] for r in rows if r[c] != NULL}
        d = len(domain)
        for h, mapping in enumerate(levels_map[c]):
            if h == chosen[c]:
                for r in rows:
                    v = r[c]
                    if v == NULL or d <= 1:
                        continue
                    label = naive_generalize(v, mapping)
                    size = sum(
                        1 for u in domain if naive_generalize(u, mapping) == label
                    )
                    total += (size - 1) / (d - 1)
    return total / (len(rows) * len(qi))


@dataclass
class BruteResult:
    feasible: list[dict[str, Any]]
    optimum: dict[str, Any] | None
    space_size: int


def brute_force_optimum(
    fixture: dict[str, Any], k: int, l: int
) -> BruteResult:
    """枚举全部组合（无剪枝），返回所有可行点与信息损失最小者。

    平局选择与被测实现一致的字典序（QI 声明顺序、层级升序、后列变化快），
    以便逐字节比较最优层级向量。
    """
    qi = qi_names(fixture)
    sens = sens_names(fixture)
    levels_map = levels_for(fixture["columns"])
    rows = normalize_rows(fixture)

    ranges = [range(len(levels_map[c])) for c in qi]
    space = 1
    for r in ranges:
        space *= len(r)

    feasible: list[dict[str, Any]] = []
    # itertools.product: 最后一列变化最快，与被测 DFS 相同
    for vec in itertools.product(*ranges):
        chosen = dict(zip(qi, vec))
        classes = naive_classes(rows, qi, sens, levels_map, chosen)
        k_ok = all(c["size"] >= k for c in classes)
        l_ok = k_ok and all(c["distinct_sensitive"] >= l for c in classes)
        point = {
            "levels": chosen,
            "vec": vec,
            "loss": naive_loss(rows, qi, levels_map, chosen),
            "class_sizes": sorted((c["size"] for c in classes), reverse=True),
            "class_distinct": [c["distinct_sensitive"] for c in classes],
            "k_feasible": k_ok,
            "l_feasible": l_ok,
        }
        if l_ok:
            feasible.append(point)

    optimum = None
    if feasible:
        optimum = min(feasible, key=lambda p: (round(p["loss"], 12), p["vec"]))
    return BruteResult(feasible=feasible, optimum=optimum, space_size=space)


def top_level_reachability(
    fixture: dict[str, Any], k: int, l: int
) -> dict[str, Any]:
    """独立计算最粗泛化下的可达性（用于 K/L_UNREACHABLE 交叉核验）。"""
    qi = qi_names(fixture)
    sens = sens_names(fixture)
    levels_map = levels_for(fixture["columns"])
    rows = normalize_rows(fixture)
    chosen = {c: len(levels_map[c]) - 1 for c in qi}
    classes = naive_classes(rows, qi, sens, levels_map, chosen)
    below_k = [c for c in classes if c["size"] < k]
    below_l = [c for c in classes if c["size"] >= k and c["distinct_sensitive"] < l]
    return {
        "levels": chosen,
        "classes": classes,
        "k_reachable": not below_k,
        "l_reachable": (not below_k) and (not below_l),
        "below_k_sizes": sorted((c["size"] for c in below_k), reverse=True),
        "below_l_distinct": [c["distinct_sensitive"] for c in below_l],
    }
