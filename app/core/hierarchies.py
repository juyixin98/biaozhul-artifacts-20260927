"""泛化层级：验证、构建与信息损失计算。

层级语义
========
* ``levels[0]`` 恒等映射：每个出现过的（含 NULL）原始值映射到自身；
* 每层给出 ``原始值 -> 该层标签`` 的完整映射；
* **包含关系（单调性）**：对任意原始值 x,y 与层级 h：
      ``map_h(x) == map_h(y)  ⇒  map_{h+1}(x) == map_{h+1}(y)``
  即上层只能"合并"，不能拆分或交叉重排。这是等价类计数随泛化单调
  变粗（类数不增）的数学保证，因此做强校验。

NULL 传播
=========
NULL 在所有层级映射为 NULL（不允许把 NULL 与真实值合并，避免缺失被
悄悄吸收进真实群体）。

信息损失指标
============
采用基于分组扩张的逐行损失（generalization loss / precision 变体，
对扇出不均衡层级也有定义）：

    loss(x 在第 h 层, 列 c)
        = ( |组(x,h)| − 1 ) / ( |域(c)| − 1 )        若 |域| > 1
        = 0                                            若 |域| = 1

总体为所有行 × 所有 QI 的平均值 ∈ [0,1]。该量对每列关于层级单调
不减，因此可安全用于剪枝；层级 0 恒为 0，顶层把所有真实值并为一组
时达到最大。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.config import Settings
from app.core.errors import FailureCode, ServiceError
from app.models import NULL_VALUE


@dataclass(frozen=True)
class Hierarchy:
    column: str
    # mappings[h][原始值] = 第 h 层标签（字符串）
    mappings: tuple[dict[str, str], ...]
    # groups[h][标签] = 该标签覆盖的原始值集合（声明域）
    groups: tuple[dict[str, frozenset[str]], ...]
    domain: frozenset[str]
    # 样本中实际出现的值域（信息损失归一化基准；domain 可以是更大的声明超集）
    observed_domain: frozenset[str]

    @property
    def height(self) -> int:
        """可用泛化层级数（0..height 可选），恒等层为 0。"""
        return len(self.mappings) - 1

    def apply(self, value: str, level: int) -> str:
        return self.mappings[level][value]

    def group_size(self, value: str, level: int) -> int:
        label = self.mappings[level][value]
        return len(self.groups[level][label])

    def observed_group_size(self, value: str, level: int) -> int:
        """该层标签覆盖的**样本内**真实值个数（NULL 单独计）。"""
        label = self.mappings[level][value]
        if value == NULL_VALUE:
            return 1
        return len(self.groups[level][label] & self.observed_domain)


def _err(col: str, code: FailureCode, msg: str, details: dict | None = None) -> ServiceError:
    d = {"column": col}
    if details:
        d.update(details)
    return ServiceError(code, msg, d)


def build_hierarchy(
    column: str,
    levels_in: Sequence[dict[str, str]],
    observed_values: set[str],
    settings: Settings,
) -> Hierarchy:
    """构建并强校验一个泛化层级。"""
    if len(levels_in) - 1 > settings.max_hierarchy_height:
        raise _err(
            column,
            FailureCode.HIERARCHY_TOO_DEEP,
            f"hierarchy depth {len(levels_in) - 1} exceeds limit "
            f"{settings.max_hierarchy_height}",
            {"depth": len(levels_in) - 1, "limit": settings.max_hierarchy_height},
        )

    # 规范化键/值。NULL 不允许出现在调用方层级中（JSON 键也无法表达 null）；
    # NULL 的恒等传播由系统在每层自动注入，调用方只需覆盖真实值。
    norm_levels: list[dict[str, str]] = []
    for h, lev in enumerate(levels_in):
        if not isinstance(lev, dict) or not lev:
            raise _err(
                column,
                FailureCode.INVALID_HIERARCHY,
                "each hierarchy level must be a non-empty mapping",
            )
        norm: dict[str, str] = {}
        for k, v in lev.items():
            ck, cv = canon_key(k), canon_key(v)
            if ck == NULL_VALUE:
                raise _err(
                    column,
                    FailureCode.INVALID_HIERARCHY,
                    "do not declare NULL in hierarchy mappings; NULL propagation is "
                    "handled automatically at every level",
                    {"level": h},
                )
            norm[ck] = cv
        norm_levels.append(norm)

    domain = set(observed_values)
    real_domain = domain - {NULL_VALUE}
    # NULL 在每一层自动恒等传播（仅当样本中确实出现 NULL 时进入映射）
    null_present = NULL_VALUE in domain
    if null_present:
        for m in norm_levels:
            m[NULL_VALUE] = NULL_VALUE

    l0 = norm_levels[0]

    # --- level 0 必须恒等，且必须覆盖样本中出现过的全部真实值。
    # 允许层级声明比当前样本更大的域（预定义字典/跨批次复用）；
    # 声明域即信息损失归一化所用的域。---
    missing = sorted(real_domain - set(l0))
    if missing:
        raise _err(
            column,
            FailureCode.HIERARCHY_NOT_COVERING,
            "level 0 mapping must cover every observed value",
            {"missing_values_count": len(missing)},
        )
    for k, v in l0.items():
        if k != v:
            raise _err(
                column,
                FailureCode.INVALID_HIERARCHY,
                "level 0 must be the identity mapping (value -> itself)",
                {"offending_key": _safe(k)},
            )

    # --- 每一层都必须覆盖同一组键，且标签合法 ---
    for h, m in enumerate(norm_levels):
        if set(m) != set(l0):
            miss = sorted(set(l0) - set(m))
            extra = sorted(set(m) - set(l0))
            raise _err(
                column,
                FailureCode.HIERARCHY_NOT_COVERING,
                f"level {h} must map exactly the same value set as level 0",
                {"missing_count": len(miss), "extra_count": len(extra)},
            )

    # --- NULL 恒等传播（任一层都不能把 NULL 与真实值合并，也不能改名）---
    if null_present:
        for h, m in enumerate(norm_levels):
            if m[NULL_VALUE] != NULL_VALUE:
                raise _err(
                    column,
                    FailureCode.INVALID_HIERARCHY,
                    f"NULL must propagate unchanged at every level (violated at level {h})",
                    {"level": h},
                )

    # --- 计算每层分组并验证包含关系（单调性）---
    def partitions(m: dict[str, str]) -> dict[str, frozenset[str]]:
        g: dict[str, set[str]] = {}
        for k, label in m.items():
            g.setdefault(label, set()).add(k)
        return {label: frozenset(s) for label, s in g.items()}

    groups: list[dict[str, frozenset[str]]] = [partitions(l0)]

    def group_of(h: int, value: str) -> frozenset[str]:
        label = norm_levels[h][value]
        return groups[h][label]

    for h in range(1, len(norm_levels)):
        cur = partitions(norm_levels[h])
        groups.append(cur)
        prev = groups[h - 1]
        # 验证：当前层每个分组必须是下层若干分组的并集。
        # 等价地：若两个值在 h-1 层同组，则在 h 层也必须同组；
        # 且 h 层标签不能把两个不同下层组的成员交叉合并后再拆开——
        # 上面条件已充分（等价关系逐加细）。
        # 实现：对每个下层分组，其成员在当前层必须全部落到同一个标签。
        for label_lo, members_lo in prev.items():
            upper_labels = {norm_levels[h][v] for v in members_lo}
            if len(upper_labels) != 1:
                raise _err(
                    column,
                    FailureCode.HIERARCHY_NOT_MONOTONE,
                    f"level {h} splits a level-{h - 1} group; hierarchy must be monotone "
                    "(upper levels may only merge groups)",
                    {"level": h, "lower_group_label": _safe(label_lo)},
                )
        # 反向包含校验：当前层每个分组确实等于若干下层分组之并（成员一致）
        for label_up, members_up in cur.items():
            covered: set[str] = set()
            for label_lo, members_lo in prev.items():
                if members_lo & members_up:
                    if not members_lo <= members_up:
                        raise _err(
                            column,
                            FailureCode.HIERARCHY_NOT_MONOTONE,
                            f"level {h} group crosses level-{h - 1} group boundaries",
                            {"level": h},
                        )
                    covered |= members_lo
            if covered != set(members_up):
                raise _err(
                    column,
                    FailureCode.HIERARCHY_NOT_MONOTONE,
                    f"level {h} group is not a union of level-{h - 1} groups",
                    {"level": h},
                )

    # 声明域 = level-0 键集（允许大于当前样本的实际取值集合）
    declared_domain = frozenset(l0)
    return Hierarchy(
        column=column,
        mappings=tuple(norm_levels),
        groups=tuple(groups),
        domain=declared_domain,
        observed_domain=frozenset(real_domain),
    )


def row_generalization_loss(hier: Hierarchy, value: str, level: int) -> float:
    """单值在某层的归一化扩张损失 ∈ [0,1]。NULL 自身成组 → 0。

    归一化基准是**样本内真实值域**大小：与独立预言机一致，且层级声明
    超集不会人为压低损失。
    """
    d = len(hier.observed_domain)
    if d <= 1 or value == NULL_VALUE:
        return 0.0
    size = hier.observed_group_size(value, level)
    return (size - 1) / (d - 1)


def total_info_loss(
    rows: Sequence[dict[str, str]],
    qi_names: Sequence[str],
    hier_map: dict[str, Hierarchy],
    levels: dict[str, int],
) -> float:
    """所有行 × 所有 QI 的平均泛化损失。"""
    if not rows or not qi_names:
        return 0.0
    total = 0.0
    for r in rows:
        for name in qi_names:
            total += row_generalization_loss(hier_map[name], r[name], levels[name])
    return total / (len(rows) * len(qi_names))


def _safe(v: str) -> str:
    """错误详情中对值做脱敏：仅给出类别/长度，不回显真实取值。"""
    if v == NULL_VALUE:
        return "<NULL>"
    return f"<value:str#len={len(v)}>"


def canon_key(v: object) -> str:
    """层级键/标签规范化（与解析层 :func:`app.models.canonical` 保持一致）。"""
    if v is None:
        return NULL_VALUE
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    s = str(v).strip()
    return NULL_VALUE if s == "" else s
