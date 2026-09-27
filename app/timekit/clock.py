"""时钟模型：RFC 3550 A.8 抖动 EWMA + 收发时钟漂移比。

所有时间为整数微秒（接收端时钟域），避免浮点累积误差。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClockModel:
    """维护一条 RTP 会话的接收端统计。

    抖动（RFC 3550 A.8）::

        D_i  = (R_i - S_i) - (R_{i-1} - S_{i-1})
        J_i  = J_{i-1} + alpha * (|D_i| - J_{i-1})

    其中 S 为 RTP 时间戳折算的“发送微秒”，R 为到达微秒。

    漂移比（接收时钟/发送时钟，仅在话峰内连续到达包上更新）::

        ratio_i = (1-beta)*ratio_{i-1} + beta * dR / dS

    ratio > 1 表示发送端时钟偏快（接收端测得包间隔更长）。
    """

    clock_rate: int
    jitter_smoothing: float = 1.0 / 16.0
    drift_smoothing: float = 1.0 / 16.0

    def __post_init__(self) -> None:
        self.jitter_us: int = 0  # 四舍五入为整数微秒
        self._jitter_float: float = 0.0
        self.spike_us: int = 0  # 本话峰观测到的最大排队延迟（相对基准 transit）
        self.clock_ratio: float = 1.0
        self._prev_arrival_us: int | None = None
        self._prev_sender_us: float | None = None
        self._min_transit: float | None = None
        self.samples_seen: int = 0
        self.continuous_packets: int = 0  # 当前话峰内连续推进的包数

    def _sender_micros(self, ts_ext: int) -> float:
        return ts_ext * 1_000_000.0 / self.clock_rate

    def update_jitter(self, *, ts_ext: int, arrival_us: int) -> int:
        """每个**新**包到达时调用（重复包不调用），返回当前抖动整数微秒。

        同时维护两项：

        - ``jitter_us``：RFC 3550 A.8 的 1/16 EWMA 抖动（稳态背景抖动）；
        - ``spike_us``：本话峰内 ``transit - min_transit`` 的峰值，即该包
          经历的排队延迟。突发乱序让窗口内包排队几百毫秒，EWMA 会平滑掉它，
          峰值项则能支撑目标延迟。
        """
        sender_us = self._sender_micros(ts_ext)
        transit = arrival_us - sender_us
        if self._min_transit is None or transit < self._min_transit:
            self._min_transit = transit
        queueing = transit - self._min_transit
        if queueing > self.spike_us:
            self.spike_us = int(round(queueing))

        if self._prev_arrival_us is not None:
            d = transit - (
                self._prev_arrival_us - self._prev_sender_us)
            self._jitter_float += self.jitter_smoothing * (
                abs(d) - self._jitter_float)

        self._prev_arrival_us = arrival_us
        self._prev_sender_us = sender_us
        self.samples_seen += 1
        self.jitter_us = int(round(self._jitter_float))
        return self.jitter_us

    def reset_spike(self) -> None:
        """话峰首包清空峰值（新话峰重新观测）；min_transit 跨话峰保留。"""
        self.spike_us = 0

    def update_drift(self, *, d_ts_samples: int, d_arrival_us: int) -> float:
        """话峰内连续到达的包上更新漂移比，返回新比值。

        ``d_ts_samples`` 为 RTP 时间戳增量（采样数），必须为正；
        ``d_arrival_us`` 为到达间隔。漂移比跨话峰保持（EWMA 不重置），
        使新话峰锚点一开始就能用上学到的收发时钟比。
        """
        if d_ts_samples <= 0 or d_arrival_us <= 0:
            return self.clock_ratio
        observed = d_arrival_us / (d_ts_samples * 1_000_000.0 / self.clock_rate)
        self.clock_ratio += self.drift_smoothing * (observed - self.clock_ratio)
        self.continuous_packets += 1
        return self.clock_ratio

    def mark_talkspurt_start(self) -> None:
        self.continuous_packets = 0

    def frame_duration_us(self, nominal_us: int) -> int:
        """按当前漂移比把一帧的发送时长折算为接收端微秒。"""
        return int(round(nominal_us * self.clock_ratio))
