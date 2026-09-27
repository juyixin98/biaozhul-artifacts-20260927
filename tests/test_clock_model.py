"""RFC3550 抖动 EWMA 与漂移比估计的具体数值断言。"""

from __future__ import annotations

import math

from app.timekit.clock import ClockModel


def test_jitter_rfc3550_known_value() -> None:
    # 构造已知的 transit 差序列，手算 J
    m = ClockModel(clock_rate=8000)
    # 第一包：只存 transit，J=0
    m.update_jitter(ts_ext=0, arrival_us=100_000)
    assert m.jitter_us == 0
    # 第二包：transit 比第一包大 2000us -> |D|=2000 -> J=125
    m.update_jitter(ts_ext=160, arrival_us=122_000)  # transit=122000-20000=102000
    assert m.jitter_us == 125  # 2000/16


def test_jitter_decays_then_spikes() -> None:
    m = ClockModel(clock_rate=8000)
    # 稳定流：每包 transit 恒定
    for i in range(20):
        m.update_jitter(ts_ext=i * 160, arrival_us=100_000 + i * 20_000)
    assert m.jitter_us == 0
    # 一包 40ms 尖峰
    m.update_jitter(ts_ext=20 * 160, arrival_us=100_000 + 20 * 20_000 + 40_000)
    assert m.jitter_us > 2000
    # 排队峰值捕获尖峰
    assert m.spike_us >= 39_000


def test_clock_ratio_converges_to_two_percent_fast() -> None:
    m = ClockModel(clock_rate=8000)
    # 接收间隔 20400us / 发送 20000us = 1.02
    for _ in range(200):
        m.update_drift(d_ts_samples=160, d_arrival_us=20_400)
    assert math.isclose(m.clock_ratio, 1.02, abs_tol=0.002)


def test_frame_duration_scaled_by_ratio() -> None:
    m = ClockModel(clock_rate=8000)
    for _ in range(100):
        m.update_drift(d_ts_samples=160, d_arrival_us=20_400)
    assert m.frame_duration_us(20_000) == round(20_000 * m.clock_ratio)
