"""泛化层级：实例化、可验证性与包含关系。

层级第 0 级恒为“原始值”；第 1..d 级来自 :class:`LevelSpec`。

校验三类问题，分别给出不同失败类别：

- ``HIERARCHY_INCOMPLETE``  数据域里的值在某一级无处可去（未覆盖、非数值、
  超出箱边界、字符串短于 prefix 长度）。
- ``HIERARCHY_KEEP_NOT_DECREASING``  多个 prefix 级的 ``keep`` 未严格递减。
- ``HIERARCHY_NOT_CONTAINING``  经验划分上，第 i 级不是第 i-1 级的合并
  （即同一上一级标签映射到了多个本级标签 / 箱没有逐级变粗）。

NULL 不属于任何层级域：它在每一级都保持 :data:`NULL`，因此 NULL 行始终
留在样本与等价类内。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..errors import ErrorCode, RiskError
from ..logging_setup import get_logger
from .types import HierarchySpec, NULL

log = get_logger("hierarchy")


@dataclass
class MaterializedHierarchy:
    column: str
    depth: int
    # level(1..d) -> 标签函数（原始值或上一级标签 -> 本级标签）
    _apply_at: dict[int, Callable[[Optional[str], Optional[str]], Optional[str]]]
    # 每一级在数据域上的标签集合（NULL 单列），用于证据与复核
    level_labels: dict[int, set[Optional[str]]]
    # 校验证据（聚合，无原始值）
    validation: dict

    def apply_level(self, level: int, raw_value: Optional[str],
                    prev_label: Optional[str]) -> Optional[str]:
        if level == 0:
            return raw_value
        return self._apply_at[level](raw_value, prev_label)


def materialize(spec: HierarchySpec,
                raw_values: list[Optional[str]]) -> MaterializedHierarchy:
    """按该列在数据集中出现的值域实例化并完整校验层级。"""
    column = spec.column
    observed = sorted({v for v in raw_values if v is not NULL})
    has_null = any(v is NULL for v in raw_values)

    apply_at: dict[int, Callable[[Optional[str], Optional[str]], Optional[str]]] = {}
    level_labels: dict[int, set[Optional[str]]] = {}
    # level 0 的“标签”就是原始值
    prev_labels: set[Optional[str]] = set(observed)
    if has_null:
        prev_labels.add(NULL)
    family: Optional[str] = None
    prev_keep: Optional[int] = None
    validation_levels: list[dict] = []

    def make_map_fn(level_index: int, mapping: dict[str, str],
                    first: bool) -> Callable:
        required_keys = set(observed) if first else {
            x for x in prev_labels if x is not NULL
        }
        missing = sorted(required_keys - set(mapping))
        unknown = sorted(set(mapping) - required_keys)
        if missing:
            raise RiskError(
                f"列 {column} 第 {level_index + 1} 级 mapping 未覆盖数据域",
                code=ErrorCode.HIERARCHY_INCOMPLETE,
                details={"column": column, "level_index": level_index,
                         "missing_key_count": len(missing)},
            )
        # 未知键：用户以为在泛化数据里不存在的标签——显式报错而非静默忽略，
        # 防止层级拼写错误被当成有效输入
        if unknown:
            raise RiskError(
                f"列 {column} 第 {level_index + 1} 级 mapping 含数据域外的键",
                code=ErrorCode.HIERARCHY_UNKNOWN_KEY,
                details={"column": column, "level_index": level_index,
                         "unknown_key_count": len(unknown)},
            )

        def fn(raw: Optional[str], prev: Optional[str]) -> Optional[str]:
            if raw is NULL or (not first and prev is NULL) or (first and raw is NULL):
                return NULL
            key = raw if first else prev
            return mapping[key]  # type: ignore[index]
        return fn

    def make_prefix_fn(level_index: int, keep: int) -> Callable:
        too_short = [v for v in observed if len(v) < keep]
        if too_short:
            raise RiskError(
                f"列 {column} 第 {level_index + 1} 级 prefix(keep={keep}) 对数据域不完整",
                code=ErrorCode.HIERARCHY_INCOMPLETE,
                details={"column": column, "level_index": level_index,
                         "short_value_count": len(too_short), "keep": keep},
            )

        def fn(raw: Optional[str], prev: Optional[str]) -> Optional[str]:
            if raw is NULL:
                return NULL
            return raw[:keep]
        return fn

    def make_range_fn(level_index: int, bins: list[float],
                      labels: Optional[list[str]]) -> Callable:
        numeric: dict[str, float] = {}
        non_numeric = 0
        out_of_range = 0
        for v in observed:
            try:
                num = float(v)
            except (TypeError, ValueError):
                non_numeric += 1
                continue
            if num < bins[0] or num > bins[-1]:
                out_of_range += 1
            numeric[v] = num
        if non_numeric:
            raise RiskError(
                f"列 {column} 第 {level_index + 1} 级 range 规则遇到非数值",
                code=ErrorCode.HIERARCHY_INCOMPLETE,
                details={"column": column, "level_index": level_index,
                         "non_numeric_count": non_numeric},
            )
        if out_of_range:
            raise RiskError(
                f"列 {column} 第 {level_index + 1} 级 bins 未覆盖全部数值",
                code=ErrorCode.HIERARCHY_INCOMPLETE,
                details={"column": column, "level_index": level_index,
                         "out_of_range_count": out_of_range,
                         "bin_low": bins[0], "bin_high": bins[-1]},
            )
        names = labels or [f"[{bins[i]},{bins[i+1]})" for i in range(len(bins) - 1)]

        def fn(raw: Optional[str], prev: Optional[str]) -> Optional[str]:
            if raw is NULL:
                return NULL
            num = numeric[raw]  # type: ignore[index]
            # 右端点闭到最后一个箱
            if num == bins[-1]:
                return names[-1]
            for i in range(len(bins) - 1):
                if bins[i] <= num < bins[i + 1]:
                    return names[i]
            return NULL  # pragma: no cover - 上面已覆盖范围校验
        return fn

    for idx, level in enumerate(spec.levels, start=1):
        if family is None:
            family = level.rule
        elif level.rule != family and level.rule != "map":
            raise RiskError(
                f"列 {column} 的层级规则族在第 {idx} 级发生变化"
                f"（{family} -> {level.rule}）；不允许混合 prefix/range 规则",
                code=ErrorCode.HIERARCHY_BAD_LEVEL,
                details={"column": column, "level_index": idx - 1},
            )

        if level.rule == "map":
            assert level.mapping is not None
            fn = make_map_fn(idx - 1, level.mapping, first=(idx == 1))
        elif level.rule == "prefix":
            assert level.keep is not None
            if family == "map":
                raise RiskError(
                    f"列 {column} 第 {idx} 级不能在 map 之后使用 prefix",
                    code=ErrorCode.HIERARCHY_BAD_LEVEL,
                    details={"column": column, "level_index": idx - 1},
                )
            if prev_keep is not None and level.keep >= prev_keep:
                raise RiskError(
                    f"列 {column} prefix 层级的 keep 必须严格递减："
                    f"{prev_keep} -> {level.keep}",
                    code=ErrorCode.HIERARCHY_KEEP_NOT_DECREASING,
                    details={"column": column, "level_index": idx - 1,
                             "previous_keep": prev_keep, "keep": level.keep},
                )
            prev_keep = level.keep
            fn = make_prefix_fn(idx - 1, level.keep)
        else:
            assert level.bins is not None
            if family == "map":
                raise RiskError(
                    f"列 {column} 第 {idx} 级不能在 map 之后使用 range",
                    code=ErrorCode.HIERARCHY_BAD_LEVEL,
                    details={"column": column, "level_index": idx - 1},
                )
            fn = make_range_fn(idx - 1, level.bins, level.labels)

        apply_at[idx] = fn

        # 在数据域上实际计算本级标签，并复核包含关系
        cur_labels: set[Optional[str]] = set()
        prev_for_value: dict[str, Optional[str]] = {}
        # 重建“原始值 -> 上一级标签”
        for v in observed:
            prev_label = v if idx == 1 else _compute_label(apply_at, idx - 1, v)
            prev_for_value[v] = prev_label
        pairs: dict[str, str] = {}
        for v in observed:
            cur = fn(v, prev_for_value[v])
            cur_labels.add(cur)
            pl = prev_for_value[v]
            assert pl is not None
            if pl in pairs and pairs[pl] != cur:
                # 经验包含复核：同一上一级标签被本级拆成了两个标签/箱
                raise RiskError(
                    f"列 {column} 第 {idx} 级破坏包含关系："
                    f"上一级标签 {pl!r} 同时落入 {pairs[pl]!r} 与 {cur!r}",
                    code=ErrorCode.HIERARCHY_NOT_CONTAINING,
                    details={"column": column, "level_index": idx - 1},
                )
            pairs.setdefault(pl, cur)
        if has_null:
            cur_labels.add(NULL)

        prev_labels = cur_labels
        level_labels[idx] = cur_labels
        validation_levels.append({
            "level_index": idx,
            "rule": level.rule,
            "distinct_labels": len(cur_labels),
            "contains_general": any(lbl == "*" for lbl in cur_labels),
            "null_preserved": has_null,
        })

    log.info(
        "层级校验通过",
        extra={"event": {"column": column, "depth": len(spec.levels),
                         "observed_values": len(observed),
                         "levels": validation_levels}},
    )
    return MaterializedHierarchy(
        column=column,
        depth=len(spec.levels),
        _apply_at=apply_at,
        level_labels=level_labels,
        validation={"column": column, "levels": validation_levels,
                    "observed_non_null_values": len(observed),
                    "null_present": has_null},
    )


def _compute_label(apply_at: dict[int, Callable], level: int,
                   raw: Optional[str]) -> Optional[str]:
    """对原始值顺序应用到指定 level。"""
    prev: Optional[str] = raw
    for li in range(1, level + 1):
        cur = apply_at[li](raw, prev)
        prev = cur
    return prev


def apply_vector(rows: list[dict], qi_columns: list[str],
                 materialized: dict[str, MaterializedHierarchy],
                 levels: dict[str, int]) -> list[tuple[Optional[str], ...]]:
    """对所有行按 (列->层级深度) 生成泛化后的 QI 键（NULL 保留）。"""
    keys: list[tuple[Optional[str], ...]] = []
    for row in rows:
        key_parts: list[Optional[str]] = []
        for col in qi_columns:
            raw = row[col]
            depth = levels[col]
            mh = materialized[col]
            if depth > mh.depth:
                raise RiskError(
                    f"列 {col} 请求第 {depth} 级，但层级只有 {mh.depth} 级",
                    code=ErrorCode.HIERARCHY_LEVEL_NOT_FOUND,
                    details={"column": col, "requested": depth, "max": mh.depth},
                )
            key_parts.append(_compute_label(mh._apply_at, depth, raw))
        keys.append(tuple(key_parts))
    return keys
