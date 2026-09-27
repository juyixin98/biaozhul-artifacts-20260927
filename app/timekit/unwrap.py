"""循环计数器按各自位宽展开为单调整数（RFC 3550 序列号/时间戳回绕）。

序号 16 位、时间戳 32 位分别独立展开，互不借用位宽。展开基于“最近展开值”
做最短距离折叠；首包以线值为基准（不假设从 0 开始）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Unwrapper:
    """通用 2**bits 回绕展开器。

    - ``unwrapped``：最近一次展开结果；
    - ``wrap_forward_events``：发送端正向跨过 0（如 65535 -> 0）的次数；
    - ``wrap_backward_events``：线值反向跳过整个半环（异常重排序/旧基线）。
    """

    bits: int
    _initialized: bool = field(default=False, init=False)
    _last: int = field(default=0, init=False)
    wrap_forward_events: int = field(default=0, init=False)
    wrap_backward_events: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.bits <= 0:
            raise ValueError("bits 必须为正整数")
        self._modulo = 1 << self.bits
        self._half = 1 << (self.bits - 1)

    def update(self, wire: int) -> int:
        if not 0 <= wire < self._modulo:
            raise ValueError(f"线值 {wire} 超出 {self.bits} 位范围")
        if not self._initialized:
            self._initialized = True
            self._last = wire
            return wire
        raw_delta = wire - (self._last % self._modulo)
        if raw_delta < -self._half:
            # 65534 -> 1：发送端正向跨过 0，真实增量为 raw + 模
            delta = raw_delta + self._modulo
            self.wrap_forward_events += 1
        elif raw_delta > self._half:
            # 1 -> 65534：跨越半环以上的反向跳变（异常，显式记录）
            delta = raw_delta - self._modulo
            self.wrap_backward_events += 1
        else:
            delta = raw_delta
        self._last += delta
        return self._last

    @property
    def unwrapped(self) -> int | None:
        return self._last if self._initialized else None

    @property
    def cycles(self) -> int:
        """展开值折算的完整周期数（用于断言“确实发生了回绕”）。"""
        if not self._initialized:
            return 0
        return self._last // self._modulo

    def reset(self) -> None:
        self._initialized = False
        self._last = 0
        self.wrap_forward_events = 0
        self.wrap_backward_events = 0
