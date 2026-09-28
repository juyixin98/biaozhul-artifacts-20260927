"""模拟时钟。

所有 TTL/过期判断只依赖这个可注入的时钟，测试与离线回放可完全确定地推进时间，
不依赖真实系统时钟的随机性。生产形态下使用 ``SystemClock``。
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)


class FakeClock:
    """确定性时钟：从固定起点开始，只能通过 :meth:`advance` 前进。"""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> int:
        if ms < 0:
            raise ValueError("时钟只能向前推进")
        self._now += ms
        return self._now
