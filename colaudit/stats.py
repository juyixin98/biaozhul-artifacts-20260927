"""列统计结构、从数据重算、页 -> 行组聚合 (验收规则 3 的聚合内核)。

统计约定:
* min/max 只在非 NaN、非 NULL 值上计算 (NaN 不计入端点);
  全页只有 NULL/NaN 时 min/max 为 None。
* null_count 只统计 NULL; NaN 是一个具体值, 单独用 nan_count 记录。
* 浮点列单独记录 has_positive_zero / has_negative_zero。
* sorted 描述该页/行组内"非 NULL 且非 NaN 值"在 NaN 之前的有序性
  (NULL 与 NaN 不参与有序性判定); desc 反向。
* min_truncated/max_truncated 表示端点字符串被截断 (见 adapter 声明)。
"""
from __future__ import annotations

import dataclasses
import math
from typing import Iterable, Sequence

from .logical import (
    LogicalType,
    cmp,
    endpoint_equal,
    is_nan,
    is_signed_zero,
    sort_key,
    zero_sign,
)

ASC = "asc"
DESC = "desc"
SORT_ORDERS = frozenset({ASC, DESC})


@dataclasses.dataclass
class ColumnStats:
    logical_type: LogicalType
    count: int = 0
    null_count: int = 0
    nan_count: int = 0
    min: object = None
    max: object = None
    has_positive_zero: bool = False
    has_negative_zero: bool = False
    min_truncated: bool = False
    max_truncated: bool = False
    sorted: str | None = None  # None=未知/未判定, "asc"/"desc"

    # ---- 序列化 -----------------------------------------------------------
    def to_json(self) -> dict:
        data = dataclasses.asdict(self)
        data["logical_type"] = self.logical_type.value
        return data

    @classmethod
    def from_json(cls, data: dict) -> "ColumnStats":
        data = dict(data)
        data["logical_type"] = LogicalType(data["logical_type"])
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    # ---- 便捷判定 ---------------------------------------------------------
    @property
    def value_count(self) -> int:
        """非 NULL 值数 (含 NaN)。"""
        return self.count - self.null_count

    @property
    def has_nan(self) -> bool:
        return self.nan_count > 0

    @property
    def all_null(self) -> bool:
        return self.count > 0 and self.null_count == self.count

    @property
    def comparable_count(self) -> int:
        """可参与 min/max 与比较的值数 (非 NULL 且非 NaN)。"""
        return self.count - self.null_count - self.nan_count


def compute_column_stats(
    values: Sequence[object], logical_type: LogicalType
) -> ColumnStats:
    """从 Python 值列表重算列统计 (审计的"事实来源")。"""
    st = ColumnStats(logical_type=logical_type, count=len(values))
    asc_ok = True
    desc_ok = True
    prev: object = None  # 上一个"可比较"值 (跳过 NULL / NaN)
    for v in values:
        if v is None:
            st.null_count += 1
            continue
        if logical_type is LogicalType.FLOAT:
            if is_nan(v):
                st.nan_count += 1
                continue
            if is_signed_zero(v):
                if zero_sign(v) == "+":
                    st.has_positive_zero = True
                else:
                    st.has_negative_zero = True
        if st.min is None or sort_key(v, logical_type) < sort_key(
            st.min, logical_type
        ):
            st.min = v
        elif (
            logical_type is LogicalType.FLOAT
            and v == 0.0
            and st.min == 0.0
            and math.copysign(1.0, v) < 0
        ):
            st.min = v
        if st.max is None or sort_key(v, logical_type) > sort_key(
            st.max, logical_type
        ):
            st.max = v
        elif (
            logical_type is LogicalType.FLOAT
            and v == 0.0
            and st.max == 0.0
            and math.copysign(1.0, v) > 0
        ):
            st.max = v
        # 有序性只在可比较值之间判定, 允许等值
        if prev is not None:
            c = cmp(v, prev, logical_type)
            if c < 0:
                asc_ok = False
            if c > 0:
                desc_ok = False
        prev = v

    if st.comparable_count > 0:
        st.sorted = ASC if asc_ok else (DESC if desc_ok else None)
    else:
        st.sorted = None
    return st


def _endpoint_min(a: object, b: object, logical_type: LogicalType) -> object:
    if a is None:
        return b
    if b is None:
        return a
    ka, kb = sort_key(a, logical_type), sort_key(b, logical_type)
    if ka < kb:
        return a
    if kb < ka:
        return b
    # 同值端点: 浮点零保留 -0.0
    if logical_type is LogicalType.FLOAT and a == 0.0 and b == 0.0:
        return a if math.copysign(1.0, a) < 0 else b
    return a


def _endpoint_max(a: object, b: object, logical_type: LogicalType) -> object:
    if a is None:
        return b
    if b is None:
        return a
    ka, kb = sort_key(a, logical_type), sort_key(b, logical_type)
    if ka > kb:
        return a
    if kb > ka:
        return b
    if logical_type is LogicalType.FLOAT and a == 0.0 and b == 0.0:
        return a if math.copysign(1.0, a) > 0 else b
    return a


def aggregate_stats(
    pages: Iterable[ColumnStats],
    *,
    count: int | None = None,
    null_count: int | None = None,
) -> ColumnStats | None:
    """把页级统计聚合为行组/文件级统计。

    计数必须逐页相加; 若提供了外部记录的 count/null_count,
    调用方负责再与聚合结果比对 (见 audit 模块)。

    截断标志聚合规则:
    * 聚合 min 若取自一个 min 被截断的页, 则聚合 min_truncated=True;
    * 任一页 max_truncated=True 且其 max 就是聚合 max, 则聚合 max_truncated=True;
    * 被更小/更大的完整端点覆盖时截断标志清除。
    """
    pages = list(pages)
    if not pages:
        return None
    logical = pages[0].logical_type
    agg = ColumnStats(logical_type=logical)
    min_trunc = False
    max_trunc = False
    for p in pages:
        if p.logical_type is not logical:
            raise TypeError("聚合要求全部页为同一逻辑类型")
        agg.count += p.count
        agg.null_count += p.null_count
        agg.nan_count += p.nan_count
        agg.has_positive_zero |= p.has_positive_zero
        agg.has_negative_zero |= p.has_negative_zero
        if p.min is not None:
            if agg.min is None or sort_key(p.min, logical) < sort_key(
                agg.min, logical
            ):
                agg.min = p.min
                min_trunc = p.min_truncated
            elif endpoint_equal(p.min, agg.min, logical):
                min_trunc = min_trunc or p.min_truncated
        if p.max is not None:
            if agg.max is None or sort_key(p.max, logical) > sort_key(
                agg.max, logical
            ):
                agg.max = p.max
                max_trunc = p.max_truncated
            elif endpoint_equal(p.max, agg.max, logical):
                max_trunc = max_trunc or p.max_truncated

    agg.min_truncated = min_trunc
    agg.max_truncated = max_trunc
    if agg.comparable_count > 0:
        agg.sorted = _aggregate_sorted(pages, logical)
    else:
        agg.sorted = None

    if count is not None and count != agg.count:
        raise ValueError(f"行数不一致: 声明 {count}, 页聚合 {agg.count}")
    if null_count is not None and null_count != agg.null_count:
        raise ValueError(
            f"NULL 计数不一致: 声明 {null_count}, 页聚合 {agg.null_count}"
        )
    return agg


def _aggregate_sorted(
    pages: list[ColumnStats], logical: LogicalType
) -> str | None:
    """页级有序性 + 页间衔接 -> 行组有序性; 无可比较值的页跳过。"""
    nonempty = [p for p in pages if p.comparable_count > 0]
    if not nonempty:
        return None
    orders = {p.sorted for p in nonempty}
    result: str | None = None
    if orders == {ASC}:
        result = ASC
    elif orders == {DESC}:
        result = DESC
    else:
        return None
    prev_max = nonempty[0].max
    prev_min = nonempty[0].min
    for p in nonempty[1:]:
        if result == ASC:
            # 允许等值: 下一页 min >= 上一页 max
            if cmp(p.min, prev_max, logical) < 0:
                return None
            prev_max = p.max
        else:
            if cmp(p.max, prev_min, logical) > 0:
                return None
            prev_min = p.min
    return result


def stats_equal(
    actual: ColumnStats,
    claimed: ColumnStats,
    *,
    compare_sorted: bool = True,
    compare_truncation: bool = True,
) -> tuple[bool, list[str]]:
    """逐字段比对两套统计, 返回 (是否完全一致, 不一致字段说明列表)。

    端点用 endpoint_equal (区分有符号零)。截断标志按独立字段比对。
    """
    diffs: list[str] = []
    if actual.logical_type is not claimed.logical_type:
        diffs.append(
            f"logical_type: actual={actual.logical_type.value} "
            f"claimed={claimed.logical_type.value}"
        )
    scalar_fields = ("count", "null_count", "nan_count")
    for name in scalar_fields:
        av, cv = getattr(actual, name), getattr(claimed, name)
        if av != cv:
            diffs.append(f"{name}: actual={av} claimed={cv}")
    if actual.has_positive_zero != claimed.has_positive_zero:
        diffs.append(
            "has_positive_zero: "
            f"actual={actual.has_positive_zero} claimed={claimed.has_positive_zero}"
        )
    if actual.has_negative_zero != claimed.has_negative_zero:
        diffs.append(
            "has_negative_zero: "
            f"actual={actual.has_negative_zero} claimed={claimed.has_negative_zero}"
        )
    for end in ("min", "max"):
        av, cv = getattr(actual, end), getattr(claimed, end)
        if not _endpoints_match(av, cv, actual.logical_type):
            diffs.append(f"{end}: actual={av!r} claimed={cv!r}")
    if compare_truncation:
        for name in ("min_truncated", "max_truncated"):
            av, cv = getattr(actual, name), getattr(claimed, name)
            if bool(av) != bool(cv):
                diffs.append(f"{name}: actual={av} claimed={cv}")
    if compare_sorted:
        if actual.sorted != claimed.sorted:
            diffs.append(f"sorted: actual={actual.sorted} claimed={claimed.sorted}")
    return (not diffs, diffs)


def _endpoints_match(
    actual: object, claimed: object, logical: LogicalType
) -> bool:
    if actual is None or claimed is None:
        return actual is None and claimed is None
    return endpoint_equal(actual, claimed, logical)
