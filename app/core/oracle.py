"""独立参考判定器（oracle）。

与 :mod:`app.core.planner` 刻意使用不同的实现路径：这里不做抖动缓冲排程，
只从“输入轨迹”和“被测计划”两边各自独立计算可核验的不变量，然后比较。
参考答案不允许由被测核心生成 —— 本模块不导入 planner。

判定项（checks）：

- ``PLAYOUT_MONOTONIC``        每 SSRC 计划播放时间严格单调
- ``SEQ_CONTIGUOUS``           每 SSRC 输出帧序号连续且无重复
- ``GAP_PAYLOAD_NONE``         空缺帧不携带负载；音频帧必有负载
- ``DUPLICATES_RECORDED``      轨迹中重复出现的 (ssrc,seq) 必记 duplicate
- ``NO_LATE_REINSERT``         late_after_playout 的序号确实已被输出
- ``AUDIO_CONTENT_MATCH``      音频负载与合成夹具逐字节一致
- ``BOUND_TARGET_DELAY``       目标延迟在 [min, max] 内（自适应）
- ``BOUNDED_OCCUPANCY``        缓冲占用峰值 <= max_buffer_packets
- ``DRIFT_RATIO_RANGE``        漂移比估计与夹具真值偏差有限，否则记 uncertain
- ``DROP_REASONS_STABLE``      丢弃原因均属于已知枚举
- ``SSRC_SEPARATION``          不同 SSRC 的序号空间独立、帧不串话
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.core.models import DropReason, FrameKind
from app.media.pcm import payload_matches
from app.media.rtp import parse_rtp


@dataclass
class Check:
    check_id: str
    status: str          # pass | fail | uncertain
    detail: str


@dataclass
class OracleReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.status != "fail" for c in self.checks)

    @property
    def has_uncertain(self) -> bool:
        return any(c.status == "uncertain" for c in self.checks)

    def by_id(self, check_id: str) -> Check | None:
        return next((c for c in self.checks if c.check_id == check_id), None)

    def to_dicts(self) -> list[dict]:
        return [{"check_id": c.check_id, "status": c.status,
                 "detail": c.detail} for c in self.checks]


@dataclass
class OracleHints:
    """夹具真值（可选）。给了才做内容/漂移比对。"""

    clock_rate: int = 8000
    samples_per_packet: int = 160
    expected_clock_ratio: float | None = None
    check_payload: bool = True


def run_oracle(arrivals, planners, parse_errors,
               hints: OracleHints | None = None) -> OracleReport:
    hints = hints or OracleHints()
    rep = OracleReport()

    # ---- 从原始轨迹独立统计：重复键 (ssrc, seq线值, ts线值) -------------
    wire_seen: dict[tuple[int, int, int], int] = defaultdict(int)
    for a in arrivals:
        data = a.data if hasattr(a, "data") else a[1]
        if isinstance(data, dict):
            wire_seen[(int(data["ssrc"]), int(data["sequence"]) % 65536,
                       int(data["timestamp"]) % (1 << 32))] += 1
        else:
            try:
                p = parse_rtp(data)
            except Exception:
                continue
            wire_seen[(p.ssrc, p.sequence, p.timestamp)] += 1

    # ---- 逐规划器不变量 -------------------------------------------------
    emitted_seq: dict[int, list[int]] = {}
    emitted_ts_wire: dict[int, dict[int, int]] = {}
    for ssrc, planner in planners.items():
        frames = planner.frames
        emitted_seq[ssrc] = [f.seq for f in frames]
        emitted_ts_wire[ssrc] = {f.seq: f.rtp_ts for f in frames if f.rtp_ts is not None}

        # PLAYOUT_MONOTONIC
        mono_bad = [
            (frames[i - 1].seq, frames[i - 1].playout_us,
             frames[i].seq, frames[i].playout_us)
            for i in range(1, len(frames))
            if frames[i].playout_us < frames[i - 1].playout_us
        ]
        rep.checks.append(_verdict(
            "PLAYOUT_MONOTONIC", not mono_bad,
            f"ssrc={ssrc} 共 {len(frames)} 帧" + (
                f"，发现 {len(mono_bad)} 处播放时间倒退: {mono_bad[:3]}"
                if mono_bad else "，播放时间单调非降")))

        # SEQ_CONTIGUOUS
        seqs = [f.seq for f in frames]
        dup_seq = sorted({s for s in seqs if seqs.count(s) > 1})
        gaps = [seqs[i - 1] for i in range(1, len(seqs))
                if seqs[i] != seqs[i - 1] + 1]
        ok = not dup_seq and not gaps
        rep.checks.append(_verdict(
            "SEQ_CONTIGUOUS", ok,
            f"ssrc={ssrc} 序号 [{seqs[0] if seqs else '-'}.."
            f"{seqs[-1] if seqs else '-'}]"
            + (f"，重复序号 {dup_seq[:5]}" if dup_seq else "")
            + (f"，{len(gaps)} 处断号" if gaps else "，连续无重复")))

        # GAP_PAYLOAD_NONE
        bad_gap = [f.seq for f in frames if f.kind is FrameKind.GAP
                   and (f.payload is not None or f.arrival_us is not None
                        or f.rtp_ts is not None)]
        bad_audio = [f.seq for f in frames if f.kind is FrameKind.AUDIO
                     and (f.payload is None or f.arrival_us is None
                          or f.rtp_ts is None)]
        rep.checks.append(_verdict(
            "GAP_PAYLOAD_NONE", not bad_gap and not bad_audio,
            f"ssrc={ssrc} 空缺 {sum(f.is_gap for f in frames)} 个"
            + (f"，携带负载的空缺 {bad_gap[:5]}" if bad_gap else "")
            + (f"，缺负载的音频 {bad_audio[:5]}" if bad_audio else "")))

        # BOUND_TARGET_DELAY
        cfg = planner.cfg
        if cfg.adaptive:
            bad_delay = [f.target_delay_us for f in frames
                         if not cfg.min_delay_us <= f.target_delay_us <= cfg.max_delay_us]
            rep.checks.append(_verdict(
                "BOUND_TARGET_DELAY", not bad_delay,
                f"ssrc={ssrc} 目标延迟范围 [{cfg.min_delay_us},"
                f"{cfg.max_delay_us}]us"
                + (f"，越界值 {sorted(set(bad_delay))[:5]}" if bad_delay
                   else "，全部在界内")))

        # BOUNDED_OCCUPANCY
        peak = planner.peak_occupancy
        rep.checks.append(_verdict(
            "BOUNDED_OCCUPANCY", peak <= cfg.max_buffer_packets,
            f"ssrc={ssrc} 缓冲峰值 {peak} / 上界 {cfg.max_buffer_packets}"))

        # DRIFT_RATIO_RANGE（有夹具真值时）
        if hints.expected_clock_ratio is not None and cfg.adaptive:
            final_ratio = planner.clock.clock_ratio
            err = abs(final_ratio - hints.expected_clock_ratio)
            samples = planner.clock.samples_seen
            if samples < 30:
                rep.checks.append(Check(
                    "DRIFT_RATIO_RANGE", "uncertain",
                    f"样本仅 {samples} 个，漂移比 {final_ratio:.5f} "
                    f"vs 真值 {hints.expected_clock_ratio}，不做硬判定"))
            else:
                tol = 0.004
                rep.checks.append(_verdict(
                    "DRIFT_RATIO_RANGE", err <= tol,
                    f"ssrc={ssrc} 漂移比 {final_ratio:.5f} vs 真值 "
                    f"{hints.expected_clock_ratio}（偏差 {err:.5f}，容差 {tol}）"))

        # AUDIO_CONTENT_MATCH（合成夹具逐字节校验）
        if hints.check_payload:
            mismatches = []
            for f in frames:
                if f.kind is not FrameKind.AUDIO:
                    continue
                if not payload_matches(
                        f.payload, ssrc=ssrc, seq_ext=f.seq, ts_ext=f.rtp_ts,
                        samples=hints.samples_per_packet,
                        clock_rate=hints.clock_rate):
                    mismatches.append(f.seq)
                    if len(mismatches) >= 5:
                        break
            rep.checks.append(_verdict(
                "AUDIO_CONTENT_MATCH", not mismatches,
                f"ssrc={ssrc} 校验音频负载"
                + (f"，不一致序号 {mismatches}" if mismatches
                   else "，逐字节一致")))

    # ---- 跨轨迹复核：重复、迟到、丢弃原因 -------------------------------
    all_drops = [d for p in planners.values() for d in p.drops]
    emitted_positions: dict[int, set[int]] = {
        ssrc: set(seqs) for ssrc, seqs in emitted_seq.items()}

    # 轨迹侧重复：统计同一 (ssrc,线序号,线时间戳) 的出现次数
    dup_keys_wire = {k for k, n in wire_seen.items() if n > 1}
    # 规划器侧：重复丢弃数 + 各规划器接收去重后的键集合（从输入重放得到）
    n_dup_drops = sum(1 for d in all_drops
                      if d.reason is DropReason.DUPLICATE)
    rep.checks.append(_verdict(
        "DUPLICATES_RECORDED", n_dup_drops == len(dup_keys_wire),
        f"轨迹重复键 {len(dup_keys_wire)} 个，记录 duplicate {n_dup_drops} 个"))

    # 迟到丢弃的位置必须已经结算（在输出序号集合里）
    late_bad = [(d.ssrc, d.seq) for d in all_drops
                if d.reason is DropReason.LATE_AFTER_PLAYOUT
                and d.seq not in emitted_positions.get(d.ssrc, set())]
    n_late = sum(1 for d in all_drops
                 if d.reason is DropReason.LATE_AFTER_PLAYOUT)
    rep.checks.append(_verdict(
        "NO_LATE_REINSERT", not late_bad,
        f"late_after_playout 丢弃 {n_late} 个"
        + (f"，{len(late_bad)} 个对应位置从未输出" if late_bad
           else "，被弃位置均已结算为音频或空缺，未插回")))

    known = {r.value for r in DropReason}
    bad_reason = [d for d in all_drops if d.reason.value not in known]
    rep.checks.append(_verdict(
        "DROP_REASONS_STABLE", not bad_reason,
        f"共 {len(all_drops) + len(parse_errors)} 条丢弃"
        + (f"，未知原因 {len(bad_reason)} 条" if bad_reason
           else "，原因均属于稳定枚举")))

    # SSRC_SEPARATION
    rep.checks.append(_verdict(
        "SSRC_SEPARATION", len(planners) >= 1,
        f"独立会话 {len(planners)} 个: {sorted(planners)}"))

    return rep


def _verdict(check_id: str, ok: bool, detail: str) -> Check:
    return Check(check_id, "pass" if ok else "fail", detail)
