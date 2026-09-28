"""独立真值 oracle: 仅用 csv 与标准库读 ground_truth.csv。

不 import colaudit、不读 Parquet、不读 claims.json —— 参考答案与
被测核心的实现完全分离。
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

RG_SIZE = 60
PAGE_SIZE = 20
PAGES_PER_RG = 3


def load_ground_truth(root: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(Path(root) / "ground_truth.csv", newline="",
              encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        for raw in reader:
            rid, score, name, active = raw
            rows.append({
                "id": int(rid),
                "score": _parse_float(score),
                "name": None if name == "" else name,
                "active": _parse_bool(active),
            })
    assert header == ["id", "score", "name", "active"]
    return rows


def _parse_float(text: str) -> float | None:
    if text == "":
        return None
    if text == "NaN":
        return float("nan")
    return float(text)


def _parse_bool(text: str) -> bool | None:
    if text == "":
        return None
    assert text in {"true", "false"}
    return text == "true"


def pages(rows: list[dict[str, Any]]):
    """产出 (rg_index, page_index, 行切片)。"""
    for rg in range(2):
        base = rg * RG_SIZE
        for p in range(PAGES_PER_RG):
            yield rg, p, rows[base + p * PAGE_SIZE:base + (p + 1) * PAGE_SIZE]


# ---------------------------------------------------------------------------
# 谓词求值 (独立实现)
# ---------------------------------------------------------------------------
def oracle_predicate(
    rows: list[dict[str, Any]],
    column: str,
    op: str,
    target: Any = None,
) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        v = r[column]
        if op == "is_null":
            hit = v is None
        elif op == "not_null":
            hit = v is not None
        else:
            if v is None:
                hit = False
            elif isinstance(v, float) and math.isnan(v):
                hit = False
            else:
                hit = {
                    "eq": v == target,
                    "ne": v != target,
                    "lt": v < target,
                    "le": v <= target,
                    "gt": v > target,
                    "ge": v >= target,
                }[op]
        if hit:
            out.append(r)
    return out


def oracle_page_can_skip(
    page_rows: list[dict[str, Any]],
    column: str,
    op: str,
    target: Any = None,
) -> bool:
    """该页是否可被 (理想、正确的) 统计剪枝跳过: 等价于页内零命中。"""
    return len(oracle_predicate(page_rows, column, op, target)) == 0
