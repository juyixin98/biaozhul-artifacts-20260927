"""抖动缓冲规划器的单元行为：重复/迟到分类、禁止插回、空缺、溢出、SSRC 隔离。

这些测试直接驱动 SessionPlanner，用精确构造的到达时刻断言具体失败类别，
不经过“可能掩盖问题”的高层场景。
"""

from __future__ import annotations

from app.config import PlannerConfig
from app.core.models import DropReason, FrameKind
from app.core.planner import InPacket, SessionPlanner

SSRC = 42
FRAME = 20_000
ANCHOR = 1_000_000 + 40_000  # 首包到达 + 40ms（固定基线）


def _p(seq: int, arrival: int, ts: int | None = None,
       ssrc: int = SSRC, marker: bool = False) -> InPacket:
    return InPacket(
        ssrc=ssrc, seq_wire=seq, ts_wire=(ts if ts is not None else seq * 160),
        arrival_us=arrival, payload=bytes([seq & 0xFF]) * 4, marker=marker)


def _drive(planner: SessionPlanner, packets: list[InPacket],
           until_us: int | None = None) -> None:
    last = until_us
    for p in packets:
        planner.run_timers_until(p.arrival_us)
        planner.ingest(p)
        last = p.arrival_us
    if last is not None:
        planner.run_timers_until(last)


def _fixed() -> SessionPlanner:
    return SessionPlanner(SSRC, PlannerConfig(
        adaptive=False, fixed_delay_us=40_000, min_delay_us=20_000))


def test_clean_stream_no_gap_no_drop() -> None:
    p = _fixed()
    pkts = [_p(i, 1_000_000 + i * FRAME) for i in range(30)]
    _drive(p, pkts)
    p.flush()
    assert p.gap_count() == 0
    assert len(p.frames) == 30
    assert all(f.kind is FrameKind.AUDIO for f in p.frames)
    playouts = [f.playout_us for f in p.frames]
    assert all(playouts[i] > playouts[i - 1] for i in range(1, 30))


def test_duplicate_classified_separately_from_late() -> None:
    p = _fixed()
    _drive(p, [_p(0, 1_000_000), _p(1, 1_020_000),
               _p(1, 1_050_000)])  # seq1 完全相同的重发
    p.flush()
    counts = p.drop_counts()
    assert counts["duplicate"] == 1
    assert counts["late_after_playout"] == 0


def test_late_packet_after_position_played_is_not_reinserted() -> None:
    p = _fixed()
    # 正常喂 seq0,1，然后直接喂 seq3,4,5（seq2 缺失）。播放时钟推进，
    # seq2 的位置先以空缺结算。
    for i in [0, 1, 3, 4, 5]:
        p.run_timers_until(1_000_000 + i * FRAME)
        p.ingest(_p(i, 1_000_000 + i * FRAME))
    # 推进到 seq2 的播放期限之后：该位置记空缺
    p.run_timers_until(ANCHOR + 2 * FRAME + 1)
    gap2 = [f for f in p.frames if f.seq == 2 and f.is_gap]
    assert len(gap2) == 1
    # 现在真正的 seq2 才到达（首次到达、负载真实，但期限已过）
    late_arrival = ANCHOR + 2 * FRAME + 5_000
    p.run_timers_until(late_arrival)
    p.ingest(_p(2, late_arrival))
    assert p.drop_counts()["late_after_playout"] == 1
    # seq2 位置保留为空缺，真实迟到音频没有插回计划
    seq2 = [f for f in p.frames if f.seq == 2]
    assert len(seq2) == 1 and seq2[0].is_gap


def test_missing_packet_becomes_explicit_gap_without_audio() -> None:
    p = _fixed()
    pkts = [_p(i, 1_000_000 + i * FRAME) for i in range(10) if i != 5]
    _drive(p, pkts, until_us=1_000_000 + 12 * FRAME)
    p.flush()
    gaps = [f for f in p.frames if f.is_gap]
    assert [g.seq for g in gaps] == [5]
    g = gaps[0]
    assert g.payload is None
    assert g.arrival_us is None
    assert g.rtp_ts is None  # 不编造时间戳，更不编造音频
    # 周围音频完好
    assert [f.seq for f in p.frames if not f.is_gap] == [
        i for i in range(10) if i != 5]


def test_target_delay_is_bounded_for_adaptive() -> None:
    p = SessionPlanner(SSRC, PlannerConfig(
        adaptive=True, min_delay_us=20_000, max_delay_us=100_000))
    # 喂入极端抖动，目标延迟不得越界
    pkts = []
    for i in range(60):
        extra = 80_000 if i % 7 == 0 else 0
        pkts.append(_p(i, 1_000_000 + i * FRAME + extra))
    _drive(p, pkts, until_us=1_000_000 + 65 * FRAME)
    for f in p.frames:
        assert 20_000 <= f.target_delay_us <= 100_000


def test_overflow_tail_drops_and_buffer_stays_bounded() -> None:
    p = SessionPlanner(SSRC, PlannerConfig(
        adaptive=False, fixed_delay_us=40_000, max_buffer_packets=20))
    # 60 包在同一时刻、按序号升序灌入：前沿随序号推进，缓冲在第 21 包起尾丢弃，
    # 因此保留连续的 seq0..19，seq20..59 被丢。
    for seq in range(60):
        p.ingest(_p(seq, 1_000_000))
    assert p.peak_occupancy <= 20
    # 60 包输入、上界 20：恰好 40 个被尾丢弃
    assert p.drop_counts()["overflow"] == 40
    p.flush()
    # 保留的 20 个为真实音频，丢弃的 40 个位置为空缺，合计 60
    assert sum(not f.is_gap for f in p.frames) == 20
    assert p.gap_count() == 40
    assert all(not f.is_gap or f.payload is None for f in p.frames)


def test_ssrc_conflict_routed_as_error() -> None:
    p = _fixed()
    p.ingest(_p(0, 1_000_000, ssrc=SSRC))
    p.ingest(_p(1, 1_020_000, ssrc=999))  # 规划器绑定 SSRC，不允许串话
    assert p.drop_counts()["ssrc_conflict"] == 1
