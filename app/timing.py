"""时间内核：毫秒 <-> 采样点 的确定性换算。

API 对外用毫秒（人类可读），信号内核内部一律用采样点（与采样率无关的确定性
整数）。换算结果向下取整，规则固定，不允许在不同模块各写一份。
"""

from __future__ import annotations

from .errors import SegmentError


def ms_to_samples(ms: float, sample_rate: int, *, name: str = "value") -> int:
    """毫秒转采样点数（向下取整，至少 0）。

    Raises:
        SegmentError: ``INVALID_ARGUMENT`` 当输入为负、非有限或采样率非法。
    """
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
        raise SegmentError(
            "INVALID_ARGUMENT",
            f"sample_rate must be a positive integer, got {sample_rate!r}",
            field="sample_rate",
        )
    try:
        ms_f = float(ms)
    except (TypeError, ValueError) as exc:
        raise SegmentError(
            "INVALID_ARGUMENT", f"{name} must be a number", field=name
        ) from exc
    if ms_f != ms_f or ms_f in (float("inf"), float("-inf")) or ms_f < 0:
        raise SegmentError(
            "INVALID_ARGUMENT",
            f"{name} must be a finite non-negative number of milliseconds",
            field=name,
            value=ms,
        )
    return int(ms_f * sample_rate / 1000.0)


def samples_to_seconds(samples: int, sample_rate: int) -> float:
    """采样点数转秒（浮点，仅用于报告展示）。"""
    return int(samples) / float(sample_rate)
