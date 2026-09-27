"""单 SSRC 抖动缓冲与播放计划规划器（离线、事件驱动）。

设计要点（对应行为契约）：

1. 16 位序号与 32 位时间戳分别由独立展开器展开；规划器绑定单一 SSRC，
   SSRC 变更在仿真层新建规划器（新会话）。
2. 重复包（同序号再次到达）与延迟包（晚于自身播放期限）分别归类；
   一旦某序号位置已经播放（音频或空缺），迟到包以 ``late_after_playout``
   丢弃，绝不插回计划。
3. 自适应目标延迟：每话峰首包计算
   ``target = clamp(k * J, min, max)``，漂移预热期内不收缩到 min 以下；
   固定基线 ``adaptive=False`` 时恒为 ``fixed_delay_us``。
4. 缺包在播放期限到达时产出 ``kind=gap`` 帧（payload 恒为 None），
   不生成任何“补帧音频”。

离线计时模型
------------
真实客户端的播放时钟独立于包到达。仿真器在每次包到达前调用
:meth:`run_timers_until`，把所有 ``deadline <= now`` 的播放位置结算：
在缓冲中 -> 音频帧；不在 -> 空缺帧。这样“延迟到达”与“重复到达”由
时序自然区分，而不是靠演示代码里的标志位。

时间单位：整数微秒（接收端时钟域）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config import PlannerConfig
from app.core.models import (
    Drop,
    DropReason,
    Frame,
    FrameKind,
    GapReason,
    PlannerSample,
)
from app.timekit.clock import ClockModel
from app.timekit.unwrap import Unwrapper


@dataclass(frozen=True)
class InPacket:
    """到达规划器的一个包（已剥离 RTP 线格式）。"""

    ssrc: int
    seq_wire: int
    ts_wire: int
    arrival_us: int
    payload: bytes
    marker: bool = False


@dataclass
class _Buffered:
    seq: int
    ts: int
    arrival_us: int
    payload: bytes
    marker: bool
    playout_us: Optional[int] = None  # 前沿推进时分配；未排程为 None


class SessionPlanner:
    def __init__(self, ssrc: int, config: PlannerConfig) -> None:
        self.ssrc = ssrc
        self.cfg = config

        self._seq_unwrap = Unwrapper(16)
        self._ts_unwrap = Unwrapper(32)
        self._clock = ClockModel(
            clock_rate=config.clock_rate,
            jitter_smoothing=config.jitter_smoothing,
            drift_smoothing=config.drift_smoothing,
        )

        self._buffer: dict[int, _Buffered] = {}
        self._received_ts: dict[int, int] = {}
        self._frontier: Optional[int] = None      # 已连续排程到的最大序号
        self._frontier_ts: Optional[int] = None
        self._highest_seq: Optional[int] = None   # 入站见过的最大序号及其 ts
        self._highest_ts: Optional[int] = None
        self._last_playout: Optional[int] = None  # 前沿序号被分配的播放时刻
        self._last_emitted: Optional[int] = None  # 已输出最后一帧的播放时刻
        self._playhead: Optional[int] = None      # 下一个待输出序号
        self._max_seen: Optional[int] = None

        self._spurt_id = -1
        self._spurt_start_seq: Optional[int] = None
        # 每个话峰的锚点表：(start_seq, anchor_playout_us, frame_step_us)
        # playhead 落后于新话峰时，旧槽仍按旧话峰锚点投影。
        self._spurt_bounds: list[tuple[int, int, int]] = []
        self._anchor_seq: Optional[int] = None       # 当前（最新）话峰锚点
        self._anchor_playout_us: Optional[int] = None
        self._spurt_frame_step: int = 0              # 当前话峰帧长快照
        self._last_step: int = 0                     # 最近一槽帧长（空缺沿用）
        self._target_delay = 0
        self._delay_ewma: Optional[float] = None
        self._spike_hold: int = 0  # 跨话峰保留的突发峰值记忆（慢放衰减）
        self._last_audio_ref: Optional[tuple[int, int]] = None
        self._last_real_ref: Optional[tuple[int, int]] = None  # (seq, ts)

        self.frames: list[Frame] = []
        self.drops: list[Drop] = []
        self.samples: list[PlannerSample] = []
        # 恢复包揭示的发送端静默区间（仅诊断；不回溯改写空缺）
        self.sender_pauses: list[dict] = []

        self._peak_occupancy = 0
        self._last_event_us: Optional[int] = None

    # ------------------------------------------------------------------ #
    # 诊断属性
    # ------------------------------------------------------------------ #

    @property
    def clock(self) -> ClockModel:
        return self._clock

    @property
    def seq_unwraps(self) -> int:
        return self._seq_unwrap.wrap_forward_events

    @property
    def ts_unwraps(self) -> int:
        return self._ts_unwrap.wrap_forward_events

    @property
    def talkspurt_count(self) -> int:
        return self._spurt_id + 1

    @property
    def peak_occupancy(self) -> int:
        return self._peak_occupancy

    def drop_counts(self) -> dict[str, int]:
        out = {r.value: 0 for r in DropReason}
        for d in self.drops:
            out[d.reason.value] += 1
        return out

    def gap_count(self) -> int:
        return sum(1 for f in self.frames if f.is_gap)

    # ------------------------------------------------------------------ #
    # 入站
    # ------------------------------------------------------------------ #

    def ingest(self, pkt: InPacket) -> None:
        if pkt.ssrc != self.ssrc:
            self.drops.append(Drop(
                DropReason.SSRC_CONFLICT, pkt.ssrc, None, pkt.arrival_us,
                f"规划器绑定 ssrc={self.ssrc} 却收到 ssrc={pkt.ssrc}"))
            return

        seq = self._seq_unwrap.update(pkt.seq_wire)
        ts = self._ts_unwrap.update(pkt.ts_wire)

        # 1) 重复 / 同序号时间戳不一致 —— 先于一切排队决策
        if seq in self._received_ts:
            if self._received_ts[seq] != ts:
                self.drops.append(Drop(
                    DropReason.TS_REGRESSION, self.ssrc, seq, pkt.arrival_us,
                    f"同序号 {seq} 的时间戳不一致: "
                    f"{self._received_ts[seq]} vs {ts}"))
            else:
                self.drops.append(Drop(
                    DropReason.DUPLICATE, self.ssrc, seq, pkt.arrival_us,
                    f"序号 {seq} 重复到达（首次到达已接收）"))
            return

        # 2) RFC 3550 抖动与排队峰值：任何“新序号”包都参与统计。
        #    迟到包尤其有信息量（正是它揭示了缓冲不够），必须计入，
        #    否则自适应永远学不到导致丢包的那次突发。
        jitter = self._clock.update_jitter(ts_ext=ts, arrival_us=pkt.arrival_us)

        # 3) 已过播放位置：禁止插回（但上面已参与统计）
        if self._playhead is not None and seq < self._playhead:
            self.drops.append(Drop(
                DropReason.LATE_AFTER_PLAYOUT, self.ssrc, seq, pkt.arrival_us,
                f"序号 {seq} 到达时播放位置已推进到 {self._playhead}，"
                f"不能插回已播放数据"))
            self._sample(pkt.arrival_us, jitter)
            return

        # 4) 缓冲硬上界：尾丢弃“播放时刻最远”的包，保护即将播放的帧
        if len(self._buffer) >= self.cfg.max_buffer_packets:
            victim_seq = max(
                self._buffer,
                key=lambda s: (self._buffer[s].playout_us is None,
                               self._buffer[s].playout_us or 0, s),
            )
            self._buffer.pop(victim_seq)
            self.drops.append(Drop(
                DropReason.OVERFLOW, self.ssrc, victim_seq, pkt.arrival_us,
                f"缓冲达到 {self.cfg.max_buffer_packets} 包硬上界，"
                f"尾丢弃最远序号 {victim_seq}"))

        self._received_ts[seq] = ts
        self._buffer[seq] = _Buffered(
            seq=seq, ts=ts, arrival_us=pkt.arrival_us,
            payload=pkt.payload, marker=pkt.marker)
        if self._max_seen is None or seq > self._max_seen:
            self._max_seen = seq

        if self._frontier is None:
            self._start_talkspurt(seq, self._buffer[seq], pkt.arrival_us)
        elif (
            seq > (self._frontier or -10**18)
            and self._last_real_ref is not None
            and self._looks_like_resume(
                ts, seq, self._last_real_ref[1], self._last_real_ref[0],
                pkt.marker)
        ):
            # 只有“向前进”的包才可能是暂停恢复；乱序补入的旧包序号落在
            # frontier 之后，走正常缓冲路径，不重锚、不误报静默。
            self._start_talkspurt(seq, self._buffer[seq], pkt.arrival_us,
                                  resume=True)
        else:
            self._advance_frontier(pkt.arrival_us)

        if self._highest_seq is None or seq > self._highest_seq:
            self._highest_seq = seq
            self._highest_ts = ts
            self._last_real_ref = (seq, ts)
        self._pump(pkt.arrival_us)
        self._sample(pkt.arrival_us, jitter)
        self._last_event_us = pkt.arrival_us

    def run_timers_until(self, now: int) -> None:
        """播放时钟推进：结算所有 deadline < now 的空缺与 <= now 的音频。

        计时器模式不设“已见序号”上界——即使暂停期间没有新包，播放时钟
        也照样前进，到期位置记为空缺。空缺判定用严格小于，使“恰好赶在
        期限时刻到达”的包不被误杀；音频判定用小于等于。
        """
        if self._frontier is not None:
            self._pump(now)
            self._sample(now, self._clock.jitter_us)

    def flush(self) -> None:
        """流结束：把已见序号全部结算（音频播放，缺失位置记空缺）。"""
        if self._frontier is None:
            return
        now = self._last_event_us or 0
        self._advance_frontier(now)
        self._pump(10**18, hard_frontier=self._max_seen)
        self._sample(now, self._clock.jitter_us)

    # ------------------------------------------------------------------ #
    # 前沿推进：为连续到达的包分配单调的播放时刻
    # ------------------------------------------------------------------ #

    def _advance_frontier(self, now: int) -> None:
        while (self._frontier + 1) in self._buffer:
            n = self._frontier + 1
            pkt = self._buffer[n]
            if pkt.marker:
                # marker 是发送端对“话峰首包”的确定性声明
                self._start_talkspurt(n, pkt, now, resume=True)
            else:
                if self.cfg.adaptive:
                    # 到达驱动排程（自适应抖动缓冲标准模型）：槽时刻取
                    #   max(包到达 + 当前目标延迟, 上一槽 + 当前漂移帧长)。
                    # 漂移比逐包更新，长话峰不会在固定网格上累积相位误差。
                    self._update_drift_for(pkt)
                    step = self._frame_step()
                    delay = self._live_target_delay()
                    t = max(pkt.arrival_us + delay,
                            self._last_playout + step)
                else:
                    # 固定延迟基线：经典静态播放网格。锚点后每槽严格按标称
                    # 帧长推进，既不跟随漂移，也不被晚到包推后——因此时钟
                    # 漂移/超深抖动会真实表现为期限错过，而不是被静默吸收。
                    step = self.cfg.frame_us_nominal
                    t = self._anchor_playout_us + (
                        n - self._anchor_seq) * step
                self._buffer[n] = self._with_playout(pkt, t)
                self._frontier = n
                self._frontier_ts = pkt.ts
                self._last_playout = t
                self._last_step = step
                self._last_real_ref = (n, pkt.ts)

    def _looks_like_resume(self, ts: int, seq: int,
                          base_ts: Optional[int], base_seq: Optional[int],
                          marker: bool) -> bool:
        """入站阶段判断一个“高于已见序号”的包是否开启新话峰。

        - marker 位：发送端显式声明；
        - 序号跳跃 >= 2：成段缺失后的恢复包（暂停/长时间静默）；
        - 时间轴断裂：ts 增量与序号增量不成比例。
        单包丢失（seq_delta==1 但实际有空洞）不在此判定，仍属同一话峰。
        """
        if marker:
            return True
        if base_ts is None or base_seq is None:
            return False
        seq_delta = seq - base_seq
        # 1) 成段序号缺失（>= 阈值）：发送端长时间静默后的恢复。
        #    阈值与缓冲容量挂钩；小窗口突发乱序由前沿缓冲吸收。
        gap_threshold = max(8, self.cfg.max_buffer_packets // 4)
        if seq_delta >= gap_threshold:
            return True
        # 2) 只有序号严格连续时，时间戳不连续才有“暂停”意义；
        #    seq_delta>1 且小于阈值的是乱序/小丢包，前沿会补洞，不重锚。
        if seq_delta == 1:
            return ts - base_ts != self.cfg.samples_per_packet
        return False

    def _start_talkspurt(self, seq: int, pkt: _Buffered, now: int,
                         resume: bool = False) -> None:
        self._spurt_id += 1
        self._clock.mark_talkspurt_start()
        # 把上一话峰观测到的峰值并入跨话峰记忆，再清空本话峰峰值。
        self._spike_hold = max(self._spike_hold, self._clock.spike_us)
        self._clock.reset_spike()
        # 话峰首包目标延迟继承跨话峰先验；话峰进行中 spike 抬升时，
        # 前沿排程经 _live_target_delay 即时上调。
        self._target_delay = self._compute_target_delay()
        self._spurt_start_seq = seq

        raw_anchor = pkt.arrival_us + self._target_delay
        if resume and self._last_real_ref is not None:
            ref_seq, ref_ts = self._last_real_ref
            seq_skip = seq - ref_seq - 1  # 跨越的缺失序号数
            # 发送时间轴可能比序号多跳（边界帧），静默时长取两者折算的较大值
            ts_extra_samples = max(
                0,
                pkt.ts - ref_ts - (seq - ref_seq) * self.cfg.samples_per_packet)
            silence_ms = (
                seq_skip * self.cfg.samples_per_packet + ts_extra_samples
            ) * 1000 / self.cfg.clock_rate
            if seq_skip > 0 or ts_extra_samples > 0:
                self.sender_pauses.append({
                    "after_seq": ref_seq,
                    "resume_seq": seq,
                    "skipped_sequence_numbers": seq_skip,
                    "silence_ms": round(silence_ms, 3),
                })
        if resume and self._last_emitted is not None:
            # 暂停恢复：恢复包不得把播放时钟往回拨
            anchor = max(raw_anchor, self._last_emitted + self._frame_step())
        else:
            # marker 话峰边界（发送端连续）：锚点由“到达 + 目标延迟”决定，
            # 但不得早于前一话峰最后一个已排程槽（保证跨话峰单调）。
            anchor = raw_anchor
            if self._frontier is not None:
                prev_last = self._slot_playout(self._frontier)
                anchor = max(anchor, prev_last + self._frame_step())

        # 话峰锚点：话峰内所有位置的播放时刻都由它按序号偏移投影，
        # 从而“迟到多久”只会决定丢/不丢，不会推迟整个播放时钟。
        self._anchor_seq = seq
        self._anchor_playout_us = anchor
        self._spurt_frame_step = self._frame_step()
        self._last_step = self._spurt_frame_step
        self._spurt_bounds.append((seq, anchor))
        if len(self._spurt_bounds) > 64:
            self._spurt_bounds = self._spurt_bounds[-64:]

        self._buffer[seq] = self._with_playout(pkt, anchor)
        self._frontier = seq
        self._frontier_ts = pkt.ts
        self._last_playout = anchor
        self._last_audio_ref = (pkt.ts, pkt.arrival_us)

    def _current_delay_need(self) -> float:
        """当前缓冲需求：稳态抖动、话峰内突发峰值、跨话峰峰值记忆三者取大。"""
        need = max(self.cfg.jitter_multiplier * self._clock.jitter_us,
                   float(self._clock.spike_us),
                   float(self._spike_hold))
        if self._clock.samples_seen <= self.cfg.drift_warmup_packets:
            need = max(need, float(self.cfg.min_delay_us))
        # 漂移残余偏差沿话峰累积的相位漂移裕度
        residual = abs(self._clock.clock_ratio - 1.0)
        expected_spurt_frames = max(1, self.cfg.drift_warmup_packets * 3)
        need += residual * expected_spurt_frames * self.cfg.frame_us_nominal
        return need

    def _compute_target_delay(self) -> int:
        if not self.cfg.adaptive:
            return self.cfg.fixed_delay_us
        need = self._current_delay_need()
        # 跨话峰 EWMA 慢放：学到的缓冲需求平滑衰减，不在干净话峰瞬间清零
        if self._delay_ewma is None:
            self._delay_ewma = need
        else:
            p = self.cfg.delay_persistence
            self._delay_ewma = p * self._delay_ewma + (1.0 - p) * need
        target = int(round(max(need, self._delay_ewma)))
        return max(self.cfg.min_delay_us,
                   min(self.cfg.max_delay_us, target))

    def _live_target_delay(self) -> int:
        """话峰内前沿排程用：随突发峰值即时上调（只升不降），钳在上下限内。"""
        if not self.cfg.adaptive:
            return self.cfg.fixed_delay_us
        need = self._current_delay_need()
        target = int(round(max(need, self._target_delay)))
        target = max(self.cfg.min_delay_us,
                     min(self.cfg.max_delay_us, target))
        self._target_delay = target
        return target

    def _frame_step(self) -> int:
        nominal = self.cfg.frame_us_nominal
        if self.cfg.adaptive:
            return self._clock.frame_duration_us(nominal)
        return nominal

    def _update_drift_for(self, pkt: _Buffered) -> None:
        """只在话峰内、严格连续且间隔正常的音频包上更新漂移比。

        乱序补入（间隔异常大）与跨话峰包被排除，避免污染 EWMA。
        """
        if not self.cfg.adaptive or self._last_audio_ref is None:
            self._last_audio_ref = (pkt.ts, pkt.arrival_us)
            return
        prev_ts, prev_arr = self._last_audio_ref
        d_ts = pkt.ts - prev_ts
        d_arr = pkt.arrival_us - prev_arr
        nominal = self.cfg.frame_us_nominal
        # 只接受接近标称间隔（±10%）的连续包，突发/乱序的异常间隔不污染漂移比。
        # 漂移 2% 时 d_arr≈1.02×nominal，落在窗内；60ms+ 突发远超上界。
        sane = nominal * 0.9 <= d_arr <= nominal * 1.1
        if d_ts == self.cfg.samples_per_packet and d_arr > 0 and sane:
            self._clock.update_drift(d_ts_samples=d_ts, d_arrival_us=d_arr)
        self._last_audio_ref = (pkt.ts, pkt.arrival_us)

    # ------------------------------------------------------------------ #
    # 播放泵
    # ------------------------------------------------------------------ #

    def _slot_playout(self, seq: int) -> int:
        """空缺槽的投影播放时刻：从最近已排程槽按当前帧长外推。

        音频槽在入站时已显式排程（到达驱动），这里只在“包缺失”时被泵调用；
        话峰刚建立、尚无已排程邻居时回退到话峰锚点。
        """
        if self._last_playout is not None and self._frontier is not None:
            return self._last_playout + (
                seq - self._frontier) * self._last_step
        start, anchor = self._spurt_bounds[-1]
        return anchor + (seq - start) * self._last_step

    def _pump(self, now: int, hard_frontier: Optional[int] = None) -> None:
        if self._frontier is None:
            return
        while True:
            n = self._playhead if self._playhead is not None else self._spurt_start_seq

            # flush 模式不越过已见序号
            if hard_frontier is not None and n > hard_frontier:
                return

            # 该序号位置的投影播放期限（从最近已排程槽外推）；
            # 空缺/迟到结算一律不早于上一输出帧 + 一帧，保证单调。
            slot_t = self._slot_playout(n)
            if self._last_emitted is not None:
                slot_t = max(slot_t, self._last_emitted + self._last_step)
            buffered = self._buffer.get(n)

            if buffered is None:
                # 包不在：期限未过可以再等；过期（或 flush 收尾）记空缺
                if hard_frontier is None and slot_t >= now:
                    return
                self._emit_gap(n, slot_t, GapReason.MISSED_AT_DEADLINE)
                self._advance_frontier(now)
                continue

            playout = buffered.playout_us
            if playout is None:
                # 前沿内部位置补入：用锚点投影排程
                playout = slot_t
                self._buffer[n] = self._with_playout(buffered, playout)

            if hard_frontier is None and playout > now:
                return  # 还没到播放时刻，等待

            # 到期结算。是否迟到只取决于“包自身的到达时刻”是否晚于槽期限，
            # 而不是泵在稍后哪个 tick 处理它——在期限前到达的包永远是真实音频。
            if buffered.arrival_us > playout and hard_frontier is None:
                self._buffer.pop(n)
                self.drops.append(Drop(
                    DropReason.LATE_AFTER_PLAYOUT, self.ssrc, n,
                    buffered.arrival_us,
                    f"序号 {n} 到达 {buffered.arrival_us}us 晚于播放期限 "
                    f"{playout}us，迟到包丢弃且不插回，位置记空缺"))
                self._emit_gap(n, slot_t, GapReason.MISSED_AT_DEADLINE)
                self._advance_frontier(now)
                continue

            self._emit_audio(n, self._buffer[n])

    def _emit_audio(self, seq: int, pkt: _Buffered) -> None:
        self._buffer.pop(seq)
        if self._playhead is None:
            self._playhead = seq
        playout = pkt.playout_us
        # 最终保护：不允许任何帧早于刚输出帧（处理跨话峰边界等极端情况）
        if self._last_emitted is not None:
            playout = max(playout, self._last_emitted + self._last_step)
        self.frames.append(Frame(
            kind=FrameKind.AUDIO, ssrc=self.ssrc, seq=seq, rtp_ts=pkt.ts,
            playout_us=playout, arrival_us=pkt.arrival_us,
            payload=pkt.payload, gap_reason=None,
            talkspurt_id=self._spurt_id, drift_ratio=self._clock.clock_ratio,
            target_delay_us=self._target_delay))
        # 注意：漂移参照 _last_audio_ref 只在前沿推进时前进，
        # 播放输出滞后于前沿，这里回写会把参照拉回旧包、污染漂移估计。
        self._last_emitted = playout
        self._playhead = seq + 1

    def _emit_gap(self, seq: int, playout_us: int, reason: GapReason) -> None:
        self.frames.append(Frame(
            kind=FrameKind.GAP, ssrc=self.ssrc, seq=seq, rtp_ts=None,
            playout_us=playout_us, arrival_us=None, payload=None,
            gap_reason=reason, talkspurt_id=self._spurt_id,
            drift_ratio=self._clock.clock_ratio,
            target_delay_us=self._target_delay))
        if self._frontier is None or seq > self._frontier:
            self._frontier = seq
            self._last_playout = playout_us
            # frontier_ts 保留为最近真实音频的 ts，供后续连续性判断
        if self._playhead is None:
            self._playhead = seq
        self._last_emitted = playout_us
        self._playhead = seq + 1

    # ------------------------------------------------------------------ #
    # 采样
    # ------------------------------------------------------------------ #

    def _sample(self, now: int, jitter: int) -> None:
        occ = len(self._buffer)
        self._peak_occupancy = max(self._peak_occupancy, occ)
        self.samples.append(PlannerSample(
            at_us=now, occupancy=occ, target_delay_us=self._target_delay,
            jitter_us=jitter, clock_ratio=self._clock.clock_ratio,
            playhead_seq=self._playhead, frontier_seq=self._frontier))

    @staticmethod
    def _with_playout(pkt: _Buffered, playout: int) -> _Buffered:
        return _Buffered(pkt.seq, pkt.ts, pkt.arrival_us, pkt.payload,
                         pkt.marker, playout)
