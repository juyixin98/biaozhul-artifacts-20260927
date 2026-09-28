"""时间 <-> 样本换算。全部以帧（混合后的采样点）为单位。"""
from __future__ import annotations


def ms_to_samples(ms: float, sample_rate: int) -> int:
    """毫秒转样本数，四舍五入（半样本向上）。"""
    if ms < 0:
        raise ValueError("duration must be non-negative")
    return int(round(ms * sample_rate / 1000.0))


def samples_to_ms(samples: int, sample_rate: int) -> float:
    return samples * 1000.0 / sample_rate


def check_sample_rate(sample_rate: int) -> None:
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool):
        raise ValueError("sample_rate must be an integer")
    if not (1 <= sample_rate <= 1_000_000):
        raise ValueError("sample_rate must be in [1, 1_000_000]")
