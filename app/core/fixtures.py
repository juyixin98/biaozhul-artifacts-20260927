"""本地合成抓包轨迹夹具（无外部参与者、无真实业务数据）。

生成 :class:`RawArrival` 轨迹，载荷由 :mod:`app.media.pcm` 确定性合成。
所有扰动由种子化 numpy 随机数驱动，轨迹可复现。

支持的扰动（对应验证夹具）：

- ``burst_holds``   突发乱序：窗口内的包被整体延后释放，后面的包先到
- ``clock_ratio``   时钟漂移：接收端测得的包间隔 = 标称 * ratio
- ``start_seq/ts``  回绕：从 16/32 位边界附近起步即可触发自然回绕
- ``pauses``        暂停重启：发送端静默期不发包，序号与 RTP 时间轴一并跳过
- ``duplicates``    重复包：稍晚再次发送完全相同的线格式
- ``jitter_us``     逐包独立均匀抖动
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.core.simulator import RawArrival, clock_tick
from app.media.pcm import sine_frame
from app.media.rtp import build_rtp

US_PER_S = 1_000_000


@dataclass
class StreamSpec:
    ssrc: int = 0x1A2B3C4D
    clock_rate: int = 8000
    samples_per_packet: int = 160
    packets: int = 200
    start_seq: int = 0
    start_ts: int = 0
    start_arrival_us: int = 1_000_000
    payload_type: int = 0
    freq_hz: float = 440.0
    clock_ratio: float = 1.0
    # (spurt_index, offset_in_spurt, hold_us)
    burst_holds: list[tuple[int, int, int]] = field(default_factory=list)
    spurt_length: int | None = None
    # (after_packet_index, pause_us)：该索引之后插入发送暂停
    pauses: list[tuple[int, int]] = field(default_factory=list)
    # (packet_index, delay_us)：该索引的包再额外发一次
    duplicates: list[tuple[int, int]] = field(default_factory=list)
    jitter_us: float = 0.0
    seed: int = 7


@dataclass
class BuiltStream:
    arrivals: list[RawArrival]
    spec: StreamSpec
    index: dict[int, tuple[int, int]] = field(default_factory=dict)


def build_stream(spec: StreamSpec) -> BuiltStream:
    rng = np.random.default_rng(spec.seed)
    frame_us = int(spec.samples_per_packet * US_PER_S / spec.clock_rate)

    seq = spec.start_seq
    ts = spec.start_ts
    clock_us = spec.start_arrival_us

    arrivals: list[RawArrival] = []
    index: dict[int, tuple[int, int]] = {}

    holds_by_spurt: dict[int, dict[int, int]] = {}
    for sp, off, hold in spec.burst_holds:
        holds_by_spurt.setdefault(sp, {})[off] = hold
    pause_after = dict(spec.pauses)
    dup_at = dict(spec.duplicates)

    def make_bytes(seq_ext: int, ts_ext: int, marker: bool) -> tuple[bytes, bytes]:
        payload = sine_frame(
            ssrc=spec.ssrc, seq_ext=seq_ext, ts_ext=ts_ext,
            samples=spec.samples_per_packet, clock_rate=spec.clock_rate,
            freq_hz=spec.freq_hz)
        data = build_rtp(
            sequence=seq_ext, timestamp=ts_ext, ssrc=spec.ssrc,
            payload=payload, marker=marker, payload_type=spec.payload_type)
        return data, payload

    def schedule(seq_ext: int, ts_ext: int, at_us: int, marker: bool,
                 packet_index: int) -> bytes:
        data, _payload = make_bytes(seq_ext, ts_ext, marker)
        arrivals.append(RawArrival(arrival_us=at_us, data=data))
        index[packet_index] = (seq_ext & 0xFFFF, ts_ext & 0xFFFFFFFF)
        if packet_index in dup_at:
            arrivals.append(RawArrival(
                arrival_us=at_us + dup_at[packet_index], data=data))
        return data

    for i in range(spec.packets):
        # 暂停在上一包之后：发送端在静默期不发任何包，因此恢复后
        #   - RTP 时间戳跳过整段（含下一帧的正常增量）
        #   - 序号同样跳过静默帧的序号（接收端视角=成段缺包）
        # 接收端播放时钟照常推进，逐帧插入时钟事件驱动空缺显式化。
        if i > 0 and (i - 1) in pause_after:
            pause_us = pause_after[i - 1]
            silence_samples = int(round(pause_us * spec.clock_rate / US_PER_S))
            n_silence_frames = round(silence_samples / spec.samples_per_packet)
            ts += silence_samples
            seq += n_silence_frames
            n_ticks = max(1, int(round(pause_us / frame_us)))
            for k in range(1, n_ticks + 1):
                arrivals.append(clock_tick(clock_us + frame_us * k))
            clock_us += pause_us

        spurt_idx = i // spec.spurt_length if spec.spurt_length else 0
        offset = i % spec.spurt_length if spec.spurt_length else i
        marker = bool(spec.spurt_length and offset == 0 and i > 0)

        nominal_gap = int(round(frame_us * spec.clock_ratio))
        if i > 0:
            clock_us += nominal_gap
        if spec.jitter_us:
            at_us = clock_us + int(rng.integers(
                -int(spec.jitter_us), int(spec.jitter_us) + 1))
        else:
            at_us = clock_us

        hold = holds_by_spurt.get(spurt_idx, {}).get(offset)
        if hold is None:
            schedule(seq, ts, at_us, marker, i)
        else:
            # 整体延后 hold 微秒再注入，形成“后面的包先到”的突发乱序
            data, _ = make_bytes(seq, ts, marker)
            arrivals.append(RawArrival(arrival_us=at_us + hold, data=data))
            index[i] = (seq & 0xFFFF, ts & 0xFFFFFFFF)
            if i in dup_at:
                arrivals.append(RawArrival(
                    arrival_us=at_us + hold + dup_at[i], data=data))

        seq += 1
        ts += spec.samples_per_packet

    arrivals.sort(key=lambda a: a.arrival_us)
    return BuiltStream(arrivals=arrivals, spec=spec, index=index)


def ssrc_switch_stream(specs: list[StreamSpec]) -> list[RawArrival]:
    """多条流交错到达：验证 SSRC 变更新建会话、互不污染。"""
    out: list[RawArrival] = []
    for s in specs:
        out.extend(build_stream(s).arrivals)
    out.sort(key=lambda a: a.arrival_us)
    return out
