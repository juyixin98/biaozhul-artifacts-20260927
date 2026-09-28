"""日期/时间戳变换的确定性测试（含负时间戳、round-trip、开区间月首）。

参考日期用独立的固定日历表，不调用被测转换函数自身来生成期望值。
"""
from __future__ import annotations

import datetime as dt

import pytest

from pruning import transforms as T


def test_negative_epoch_known_anchors():
    # 独立锚点：-1 秒 = 1969-12-31 23:59:59 UTC
    assert T.seconds_to_civil(-1) == (1969, 12, 31)
    # -86400 = 1969-12-31 00:00 UTC
    assert T.seconds_to_civil(-86_400) == (1969, 12, 31)
    # 1969-01-01 距 epoch 恰好 -365 天
    assert T.seconds_to_civil(-365 * 86_400) == (1969, 1, 1)
    assert T.seconds_to_civil(0) == (1970, 1, 1)


def test_positive_anchor_against_stdlib_utc():
    # 用标准库 UTC 独立核对一批正时间戳
    for days in (1, 100, 10000, 20000):
        secs = days * 86_400
        y, m, d = T.seconds_to_civil(secs)
        ref = dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc).date()
        assert (y, m, d) == (ref.year, ref.month, ref.day)


def test_civil_roundtrip_for_negative_years():
    for y in (-5, 1, 1969, 2024, 9999):
        for m in (1, 6, 12):
            d = 15
            assert T.civil_from_days(T.days_from_civil(y, m, d)) == (y, m, d)


def test_month_key_ordering_and_parse():
    keys = [T.month_key(y, m) for (y, m) in
            [(1969, 12), (1970, 1), (2024, 1), (2024, 2), (2024, 12)]]
    assert keys == sorted(keys)  # 固定宽度字典序 == 时间序
    assert T.parse_month_key("2024-02") == (2024, 2)
    with pytest.raises(ValueError):
        T.parse_month_key("2024-13")


def test_date_window_known_values():
    lo, hi = T.date_range_epoch_bounds("2024-02-01", "2024-02-01")
    assert lo == 1_706_745_600            # 独立已知值
    assert hi - lo == 86_400
    # 闰日 2024-02-29 的次日是 2024-03-01
    _, hi29 = T.date_range_epoch_bounds("2024-02-29", "2024-02-29")
    assert T.seconds_to_civil(hi29)[:2] == (2024, 3)


def test_add_months_across_year_boundary():
    assert T.add_months(2024, 12, 1) == (2025, 1)
    assert T.add_months(2024, 1, -1) == (2023, 12)


def test_invalid_date_rejected():
    with pytest.raises(ValueError):
        T.parse_date("2024-02-30")
    with pytest.raises(ValueError):
        T.parse_date("2023-02-29")
