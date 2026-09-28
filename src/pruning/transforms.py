"""固定时区与日期/时间戳变换（dateconv-1.0.0）。

规则冻结点：
1. 时间戳一律是 **UTC epoch 秒**（可负、可带小数），绝不读取系统本地时区；
2. 月分区桶值是原列的 civil-date 变换结果 ``YYYY-MM``，不是原值；
3. civil-date 用 Howard Hinnant 的 days_from_civil 互逆算法（纯整数），
   因此 1970 年以前的负时间戳结果确定、可复现。

桶值是固定宽度字符串，可按字典序比较（年份 >= 1000 时与时间序一致）。
"""
from __future__ import annotations

from .versions import DATE_TRANSFORM_VERSION, FIXED_TIMEZONE

_SECONDS_PER_DAY = 86_400


def days_from_civil(year: int, month: int, day: int) -> int:
    """civil 日期 -> 距 1970-01-01 的天数（支持负年份）。"""
    y = year - (1 if month <= 2 else 0)
    era = y // 400 if y >= 0 else (y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def civil_from_days(days: int) -> tuple[int, int, int]:
    """距 1970-01-01 的天数 -> civil 日期 (year, month, day)。"""
    z = days + 719_468
    era = z // 146_097 if z >= 0 else (z - 146_096) // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    y += 1 if m <= 2 else 0
    return y, m, d


def seconds_to_civil(ts: float) -> tuple[int, int, int]:
    """UTC epoch 秒（可负）-> (year, month, day)。"""
    days = int(ts // _SECONDS_PER_DAY)  # 向下取整，负时间戳也正确
    return civil_from_days(days)


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month_key(key: str) -> tuple[int, int]:
    """'2024-02' -> (2024, 2)，带基本校验。"""
    try:
        ys, ms = key.split("-")
        y, m = int(ys), int(ms)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"非法月桶值 {key!r}，应为 YYYY-MM") from exc
    if not 1 <= m <= 12:
        raise ValueError(f"非法月桶值 {key!r}：月份越界")
    return y, m


def parse_date(literal: str) -> tuple[int, int, int]:
    """'YYYY-MM-DD'（UTC civil 日期）-> (y, m, d)。"""
    try:
        ys, ms, ds = literal.split("-")
        y, m, d = int(ys), int(ms), int(ds)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"非法日期字面量 {literal!r}，应为 YYYY-MM-DD") from exc
    if not 1 <= m <= 12 or civil_from_days(days_from_civil(y, m, d)) != (y, m, d):
        raise ValueError(f"非法日期 {literal!r}")
    return y, m, d


def month_key_of_date(literal: str) -> str:
    y, m, _ = parse_date(literal)
    return month_key(y, m)


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """月份整数加减，返回 (year, month)。"""
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def month_start_epoch(year: int, month: int) -> int:
    """该月 1 日 00:00:00 UTC 的 epoch 秒（桶的时间下界）。"""
    return days_from_civil(year, month, 1) * _SECONDS_PER_DAY


def month_range_epoch_bounds(start_key: str, end_key_inclusive: str) -> tuple[int, int]:
    """月桶闭区间 -> epoch 秒 [lo, hi_exclusive)。

    用于把月桶值映射回原列（epoch 秒）所在的保守区间做交叉校验/展示，
    注意边界月份内部日期不精确——这正是要保守保留边界桶的原因。
    """
    sy, sm = parse_month_key(start_key)
    ey, em = parse_month_key(end_key_inclusive)
    if (sy, sm) > (ey, em):
        raise ValueError("月桶区间起点晚于终点")
    ny, nm = add_months(ey, em, 1)
    return month_start_epoch(sy, sm), month_start_epoch(ny, nm)


def date_range_epoch_bounds(start_date: str, end_date_inclusive: str) -> tuple[int, int]:
    """日期闭区间 -> epoch 秒 [lo, hi_exclusive)（次日 00:00 UTC）。"""
    sy, sm, sd = parse_date(start_date)
    ey, em, ed = parse_date(end_date_inclusive)
    lo = days_from_civil(sy, sm, sd) * _SECONDS_PER_DAY
    hi = (days_from_civil(ey, em, ed) + 1) * _SECONDS_PER_DAY
    if lo >= hi:
        raise ValueError("日期区间起点晚于终点")
    return lo, hi


def transform_identity() -> dict:
    return {
        "name": "month",
        "version": DATE_TRANSFORM_VERSION,
        "timezone": FIXED_TIMEZONE,
        "source_granularity": "utc_epoch_seconds",
        "bucket": "civil_month_YYYY-MM",
    }
