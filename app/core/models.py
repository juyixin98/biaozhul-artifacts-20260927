"""播放计划的事件模型与失败原因（错误语义的唯一真源）。"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class FrameKind(str, enum.Enum):
    AUDIO = "audio"          # 真实到达的音频帧（负载经校验）
    GAP = "gap"              # 显式空缺标记，不携带任何合成音频


class GapReason(str, enum.Enum):
    """空缺产生原因 —— 失败类别单列，不与正常帧混在一起。"""

    MISSED_AT_DEADLINE = "missed_at_deadline"
    # 播放期限到达仍未到包：缓冲“有意放弃”等待（不是音频）
    LOST_WITHOUT_SSRC_CONTEXT = "lost_without_ssrc_context"
    # 话峰内序号不连续（发送端声明的跳跃，无对应包）
    UNKNOWN = "unknown"


class DropReason(str, enum.Enum):
    """入站/出站丢弃原因。"""

    DUPLICATE = "duplicate"
    # 同一 (ssrc, 序号) 已接收过；重复包与延迟包分别统计
    LATE_AFTER_PLAYOUT = "late_after_playout"
    # 到达晚于自身播放期限（对应位置已播放或是空缺）：禁止插回
    OVERFLOW = "overflow"
    # 超过缓冲硬上界 max_buffer_packets
    SSRC_CONFLICT = "ssrc_conflict"
    # 单 SSRC 规划器收到别的 SSRC 报文（路由层错误，不是新建会话）
    PARSE_ERROR = "parse_error"
    # 报文不合规（版本/长度/填充等）
    TS_REGRESSION = "ts_regression"
    # 同序号重复但 RTP 时间戳不一致（异常，拒绝并记录）


@dataclass(frozen=True)
class Frame:
    """播放计划中的一帧（20ms）。"""

    kind: FrameKind
    ssrc: int
    seq: int                 # 展开后的序号
    rtp_ts: int | None       # 展开后的 RTP 时间戳（空缺为 None）
    playout_us: int          # 计划播放时刻（接收端微秒，单调非降）
    arrival_us: int | None   # 真实到达时刻（空缺为 None）
    payload: bytes | None    # AUDIO 时为真实负载；GAP 恒为 None
    gap_reason: GapReason | None = None
    talkspurt_id: int = -1
    drift_ratio: float = 1.0
    target_delay_us: int = 0

    @property
    def is_gap(self) -> bool:
        return self.kind is FrameKind.GAP


@dataclass(frozen=True)
class Drop:
    reason: DropReason
    ssrc: int
    seq: int | None          # 展开序号；解析失败可能没有
    arrival_us: int
    detail: str = ""


@dataclass(frozen=True)
class PlannerSample:
    """过程采样点，用于“缓冲有界/延迟自适应”诊断与断言。"""

    at_us: int
    occupancy: int
    target_delay_us: int
    jitter_us: int
    clock_ratio: float
    playhead_seq: int | None
    frontier_seq: int | None
