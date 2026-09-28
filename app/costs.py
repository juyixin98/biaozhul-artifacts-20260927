"""加权编辑操作的代价模型。

支持范围（刻意收窄，见 README“关键取舍”）：
- insert / delete / transpose：按操作类型统一计价；
- substitute：统一默认价，外加有向字符对覆盖表（允许非对称，例如 sub('0','o')
  与 sub('o','0') 可分别配置）；
- match 固定为 0；
- 所有代价必须为有限非负数。

注意：按具体字符变化的插入/删除代价**不在支持范围内**，因为非限制性
Damerau-Levenshtein 的 O(nm) last-occurrence 递推要求这两类代价均匀
（递推式中它们以 ``(i-k-1) * w_del`` / ``(j-l-1) * w_ins`` 形式出现）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import PROJECT_ROOT


class CostConfigurationError(ValueError):
    """代价配置非法（负数、NaN、结构错误等）。"""


def _require_non_negative_finite(value: float, name: str) -> float:
    fvalue = float(value)
    if fvalue != fvalue or fvalue in (float("inf"), float("-inf")):
        raise CostConfigurationError(f"{name} 必须是有限数")
    if fvalue < 0.0:
        raise CostConfigurationError(f"{name} 必须非负，得到 {fvalue}")
    return fvalue


@dataclass(frozen=True)
class CostProfile:
    insert: float
    delete: float
    substitute: float
    transpose: float
    substitute_table: Mapping[str, Mapping[str, float]]

    def sub_cost(self, src_char: str, tgt_char: str) -> float:
        row = self.substitute_table.get(src_char)
        if row is not None and tgt_char in row:
            return float(row[tgt_char])
        return self.substitute

    def min_count_op_cost(self) -> float:
        """改变字符计数向量的最便宜单操作代价（剪枝下界使用）。"""
        table_values = [
            float(v)
            for row in self.substitute_table.values()
            for v in row.values()
        ]
        return min([self.insert, self.delete, self.substitute, *table_values])

    def min_indel_cost(self) -> float:
        return min(self.insert, self.delete)

    def to_public_dict(self) -> dict:
        return {
            "insert": self.insert,
            "delete": self.delete,
            "substitute": self.substitute,
            "transpose": self.transpose,
            "substitute_table": {
                src: dict(row) for src, row in self.substitute_table.items()
            },
        }


def load_cost_profile() -> CostProfile:
    env_path = os.environ.get("SPELLCHECK_COSTS")
    if env_path:
        path = Path(env_path)
    else:
        path = PROJECT_ROOT / "config" / "costs.json"
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return cost_profile_from_dict(raw)


def cost_profile_from_dict(raw: Mapping[str, object]) -> CostProfile:
    try:
        insert = _require_non_negative_finite(raw["insert"], "insert")
        delete = _require_non_negative_finite(raw["delete"], "delete")
        substitute = _require_non_negative_finite(raw["substitute"], "substitute")
        transpose = _require_non_negative_finite(raw["transpose"], "transpose")
    except KeyError as exc:
        raise CostConfigurationError(f"缺少代价字段: {exc.args[0]}") from exc

    table_raw = raw.get("substitute_table", {})
    if not isinstance(table_raw, Mapping):
        raise CostConfigurationError("substitute_table 必须是对象")
    table: dict[str, dict[str, float]] = {}
    for src, row_raw in table_raw.items():
        if not isinstance(src, str) or len(src) != 1:
            raise CostConfigurationError("substitute_table 的源键必须是单字符")
        if not isinstance(row_raw, Mapping):
            raise CostConfigurationError(f"substitute_table[{src!r}] 必须是对象")
        row: dict[str, float] = {}
        for tgt, value in row_raw.items():
            if not isinstance(tgt, str) or len(tgt) != 1:
                raise CostConfigurationError("substitute_table 的目标键必须是单字符")
            row[tgt] = _require_non_negative_finite(value, f"substitute_table[{src}][{tgt}]")
        table[src] = row

    return CostProfile(
        insert=insert,
        delete=delete,
        substitute=substitute,
        transpose=transpose,
        substitute_table=table,
    )
