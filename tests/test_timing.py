"""时间内核换算测试。"""

from __future__ import annotations

import pytest

from app.errors import SegmentError
from app.timing import ms_to_samples, samples_to_seconds


@pytest.mark.parametrize(
    "ms,rate,expected",
    [
        (0, 1000, 0),
        (500, 1000, 500),
        (1, 1000, 1),
        (1, 44100, 44),          # 向下取整
        (2.5, 1000, 2),
        (300, 8000, 2400),
    ],
)
def test_ms_to_samples(ms, rate, expected):
    assert ms_to_samples(ms, rate) == expected


@pytest.mark.parametrize("ms", [-1, float("nan"), float("inf"), float("-inf")])
def test_ms_to_samples_rejects_bad_ms(ms):
    with pytest.raises(SegmentError) as ei:
        ms_to_samples(ms, 1000, name="t")
    assert ei.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("rate", [0, -8000])
def test_ms_to_samples_rejects_bad_rate(rate):
    with pytest.raises(SegmentError) as ei:
        ms_to_samples(10, rate)
    assert ei.value.code == "INVALID_ARGUMENT"


def test_seconds_display():
    assert samples_to_seconds(500, 1000) == 0.5
