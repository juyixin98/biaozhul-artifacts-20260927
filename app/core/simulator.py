"""离线仿真引擎：事件循环驱动规划器，按 SSRC 路由到独立会话。

输入是“抓包轨迹”（报文 + 到达时刻），不做任何网络 IO。每个到达时刻：

1. 先推进所有已有会话的播放时钟到该时刻（结算到期音频/空缺）；
2. 再入站该时刻的包（同刻包按序号升序处理）。

新 SSRC 出现即新建 :class:`SessionPlanner`（新会话），旧会话状态完整保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.config import PlannerConfig
from app.core.models import Drop, DropReason
from app.core.planner import InPacket, SessionPlanner
from app.media.rtp import RtpParseError, parse_rtp


@dataclass(frozen=True)
class RawArrival:
    arrival_us: int
    data: bytes | None
    # 对已经解析过的合成夹具可直接给字段，避免再绕一圈线格式
    parsed: Optional[dict] = None

    @property
    def is_tick(self) -> bool:
        return self.data is None and self.parsed is None


def clock_tick(at_us: int) -> RawArrival:
    """纯播放时钟推进事件：暂停期间没有包到达，但播放时钟必须继续走。"""
    return RawArrival(arrival_us=at_us, data=None)


@dataclass
class SimResult:
    planners: dict[int, SessionPlanner]
    parse_errors: list[Drop] = field(default_factory=list)
    timer_ticks: int = 0

    @property
    def frames(self):
        return [f for p in self.planners.values() for f in p.frames]

    @property
    def drops(self):
        return self.parse_errors + [
            d for p in self.planners.values() for d in p.drops
        ]


def simulate(arrivals: Iterable[RawArrival | tuple[int, bytes]],
             config: PlannerConfig) -> SimResult:
    """对一条抓包轨迹运行抖动缓冲仿真。

    ``arrivals`` 元素为 ``RawArrival`` 或 ``(arrival_us, rtp_bytes)``；
    轨迹无需预先排序，引擎按 (时刻, 序号) 稳定排序。
    """
    events = _normalize(arrivals)
    planners: dict[int, SessionPlanner] = {}
    parse_errors: list[Drop] = []

    for at_us, batch in events:
        # 1) 先入站本时刻的包（已按序号升序），保证“恰好赶上期限”的包
        #    不会被同刻的播放计时器误判为空缺
        for item in batch:
            if item is None:
                continue  # 纯播放时钟推进事件（暂停期）
            if isinstance(item, RtpParseError):
                parse_errors.append(Drop(
                    DropReason.PARSE_ERROR, ssrc=0, seq=None,
                    arrival_us=at_us, detail=f"{item.reason}: {item.detail}"))
                continue
            planner = planners.get(item.ssrc)
            if planner is None:
                planner = SessionPlanner(item.ssrc, config)
                planners[item.ssrc] = planner
            planner.ingest(InPacket(
                ssrc=item.ssrc, seq_wire=item.sequence, ts_wire=item.timestamp,
                arrival_us=at_us, payload=item.payload, marker=item.marker))

        # 2) 再推进所有会话的播放时钟到本时刻（deadline == now 也结算）
        for planner in planners.values():
            planner.run_timers_until(at_us)

    for planner in planners.values():
        planner.flush()

    return SimResult(planners=planners, parse_errors=parse_errors,
                     timer_ticks=len(events))


def _normalize(arrivals):
    raw: list[tuple[int, object]] = []
    for a in arrivals:
        if isinstance(a, RawArrival):
            if a.is_tick:
                raw.append((a.arrival_us, None))
            elif a.parsed is not None:
                raw.append((a.arrival_us, a.parsed))
            else:
                raw.append((a.arrival_us, a.data))
        else:
            raw.append((int(a[0]), a[1]))

    enriched: list[tuple[int, object]] = []
    for at_us, payload in raw:
        if payload is None:
            enriched.append((at_us, None))  # 时钟推进
            continue
        if isinstance(payload, dict):
            enriched.append((at_us, _Preparsed(payload)))
            continue
        try:
            pkt = parse_rtp(payload)
        except RtpParseError as exc:
            enriched.append((at_us, exc))
            continue
        enriched.append((at_us, pkt))

    # 同刻批处理：包按线序号升序（时钟事件与解析失败排最前）
    def _sort_key(e):
        item = e[1]
        seq = getattr(item, "sequence", 0)
        return (e[0], seq if isinstance(seq, int) else 0)

    enriched.sort(key=_sort_key)

    grouped: dict[int, list[object]] = {}
    order: list[int] = []
    for at_us, item in enriched:
        if at_us not in grouped:
            grouped[at_us] = []
            order.append(at_us)
        grouped[at_us].append(item)
    return [(t, grouped[t]) for t in order]


class _Preparsed:
    """合成夹具的直通表示（字段语义等同 RtpPacket）。"""

    __slots__ = ("ssrc", "sequence", "timestamp", "payload", "marker",
                 "payload_type")

    def __init__(self, d: dict) -> None:
        self.ssrc = int(d["ssrc"])
        self.sequence = int(d["sequence"])
        self.timestamp = int(d["timestamp"])
        self.payload = d.get("payload", b"")
        self.marker = bool(d.get("marker", False))
        self.payload_type = int(d.get("payload_type", 0))
