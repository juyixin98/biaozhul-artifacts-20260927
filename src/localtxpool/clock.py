"""时间源。

内核与存储永远不直接调用 ``time.time()``：所有时间判断都经由 :class:`Clock`。
生产环境使用墙钟；离线回放使用 :class:`VirtualClock`，由事件文件里的 ``at``
字段推进，因此回放结果确定、可重复、可与黄金文件逐字节比对。
"""

from __future__ import annotations

import time


class Clock:
    def now(self) -> int:
        """返回当前 Unix 秒（整数，过期边界确定）。"""
        return int(time.time())


class VirtualClock:
    """由测试/回放显式设置时间的时钟；只会前进，不会后退。"""

    def __init__(self, start: int = 0):
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def advance_to(self, ts: int) -> int:
        if ts < self._now:
            raise ValueError(f"virtual clock cannot move backwards: {ts} < {self._now}")
        self._now = int(ts)
        return self._now

    def advance(self, seconds: int) -> int:
        if seconds < 0:
            raise ValueError("advance requires non-negative delta")
        self._now += int(seconds)
        return self._now
