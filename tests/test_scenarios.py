"""内置场景端到端断言：播放单调、缓冲有界、各类丢弃原因、自适应 vs 固定基线。

参考答案（oracle）独立于被测规划器，断言写具体数字与失败类别。
"""

from __future__ import annotations

import pytest

from app.core.scenarios import (
    SCENARIOS,
    scenario_burst_reorder,
    scenario_clock_drift,
    scenario_duplicates,
    scenario_overflow,
    scenario_pause_resume,
    scenario_ssrc_switch,
    scenario_wraparound,
)
from app.core.simulator import simulate
from app.core.fixtures import StreamSpec, build_stream
from app.config import PlannerConfig


def _assert_status(report, check_id: str, status: str = "pass") -> None:
    row = next(a for a in report.assertions if a["id"] == check_id)
    assert row["status"] == status, f"{check_id}: {row['detail']}"


def test_all_named_scenarios_pass() -> None:
    for name, fn in SCENARIOS.items():
        report = fn()
        fails = [a for a in report.assertions if a["status"] == "fail"]
        assert not fails, f"{name} 失败: {[(f['id'], f['detail']) for f in fails]}"
        for arm in ("adaptive", "fixed"):
            for c in report.oracle_checks[arm]:
                # uncertain 允许（样本不足等），fail 不允许
                assert c["status"] != "fail", f"{name}/{arm}/{c['check_id']}: {c['detail']}"


def test_burst_adaptive_outperforms_fixed_after_learning() -> None:
    r = scenario_burst_reorder()
    a, f = r.arms["adaptive"], r.arms["fixed"]
    _assert_status(r, "burst.cold_start_pays")
    _assert_status(r, "burst.adaptive_learns")
    _assert_status(r, "burst.target_delay_rose")
    # 具体数字：自适应只在冷启动话峰付出代价
    assert a.drops["late_after_playout"] == 1
    # 固定 40ms 不学习，至少在 3 个后续话峰各丢一次
    assert f.drops["late_after_playout"] >= 3
    # 自适应目标延迟确实抬升到能吸收 120ms 突发
    assert max(a.target_delays) >= 100_000
    assert max(f.target_delays) == 40_000  # 固定基线恒定


def test_clock_drift_learned_without_loss() -> None:
    r = scenario_clock_drift()
    _assert_status(r, "drift.ratio_learned")
    _assert_status(r, "drift.adaptive_no_loss")
    a = r.arms["adaptive"]
    assert abs(a.final_ratio - 1.02) <= 0.004
    assert a.drops["late_after_playout"] == 0
    assert a.gaps == 0


def test_wraparound_expands_independent_widths() -> None:
    r = scenario_wraparound()
    _assert_status(r, "wrap.contents.adaptive")
    _assert_status(r, "wrap.events")
    # 直接从规划器取证：16 位与 32 位都至少正向回绕一次
    spec = StreamSpec(packets=80, start_seq=65530,
                      start_ts=0xFFFFFF00, spurt_length=None, seed=31)
    res = simulate(build_stream(spec).arrivals, PlannerConfig(adaptive=True))
    p = next(iter(res.planners.values()))
    assert p.seq_unwraps >= 1
    assert p.ts_unwraps >= 1
    # 回绕后帧仍按连续展开序号输出，负载由 oracle 逐字节校验
    seqs = [fr.seq for fr in p.frames]
    assert seqs == list(range(65530, 65530 + 80))


def test_pause_resume_gaps_are_explicit_and_explained() -> None:
    r = scenario_pause_resume()
    _assert_status(r, "pause.gaps_explicit")
    _assert_status(r, "pause.audio_after_resume")
    _assert_status(r, "pause.explained")
    a = r.arms["adaptive"]
    assert a.gaps == 75
    assert a.audio == 200  # 没有合成音频充数
    pause = a.sender_pauses[0]
    assert pause["skipped_sequence_numbers"] == 75
    assert pause["silence_ms"] == 1500.0


def test_duplicates_counted_and_never_become_gaps() -> None:
    r = scenario_duplicates()
    a = r.arms["adaptive"]
    assert a.drops["duplicate"] == 3
    assert a.gaps == 0
    assert a.audio == 120


def test_overflow_buffer_bounded_and_accounted() -> None:
    r = scenario_overflow()
    _assert_status(r, "overflow.bounded.adaptive")
    _assert_status(r, "overflow.drops.adaptive")
    _assert_status(r, "overflow.accounted.adaptive")
    a = r.arms["adaptive"]
    assert a.peak_occupancy <= 250
    assert a.drops["overflow"] == 50
    assert a.audio + a.gaps == 300


def test_ssrc_switch_creates_two_independent_sessions() -> None:
    r = scenario_ssrc_switch()
    _assert_status(r, "ssrc.two_sessions")
    a = r.arms["adaptive"]
    assert a.audio == 160 and a.gaps == 0


def test_every_scenario_has_strict_monotonic_playout_and_bounded_buffer() -> None:
    for name, fn in SCENARIOS.items():
        r = fn()
        for arm_name in ("adaptive", "fixed"):
            arm = r.arms[arm_name]
            assert arm.playout_strict_monotonic, f"{name}/{arm_name} 播放非单调"
            assert arm.peak_occupancy <= 250, f"{name}/{arm_name} 缓冲越界"
