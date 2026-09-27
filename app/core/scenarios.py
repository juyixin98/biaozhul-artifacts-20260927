"""验证场景：突发乱序、时钟漂移、回绕、暂停重启、重复、溢出、SSRC 切换。

每个场景同时跑自适应规划器与固定延迟基线（``fixed_delay_us``），
产出对比统计 + oracle 复核 + 场景级断言。断言写“具体结果与失败类别”，
不写“接口可调用”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.config import PlannerConfig
from app.core.fixtures import BuiltStream, StreamSpec, build_stream, ssrc_switch_stream
from app.core.models import DropReason
from app.core.oracle import OracleHints, run_oracle
from app.core.simulator import RawArrival, simulate

US_PER_S = 1_000_000


# ----------------------------------------------------------------------- #
# 报告结构
# ----------------------------------------------------------------------- #


@dataclass
class ArmStats:
    name: str
    frames: int
    audio: int
    gaps: int
    drops: dict[str, int]
    peak_occupancy: int
    final_jitter_us: int
    final_ratio: float
    target_delays: list[int]
    playout_strict_monotonic: bool
    sender_pauses: list[dict] = field(default_factory=list)


@dataclass
class ScenarioReport:
    scenario: str
    description: str
    arms: dict[str, ArmStats]
    oracle_checks: dict[str, list[dict]]
    arrivals: int
    config: dict
    assertions: list[dict]
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(a["status"] != "fail" for a in self.assertions)

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "description": self.description,
            "passed": self.passed,
            "arrivals": self.arrivals,
            "arms": {k: v.__dict__ for k, v in self.arms.items()},
            "oracle_checks": self.oracle_checks,
            "assertions": self.assertions,
            "notes": self.notes,
            "config": self.config,
        }


# ----------------------------------------------------------------------- #
# 核心运行
# ----------------------------------------------------------------------- #


def _stats(name: str, result, fixed: bool) -> ArmStats:
    frames = result.frames
    delays = sorted({f.target_delay_us for f in frames})
    # 播放单调性是每 SSRC（每会话）语义，不能把不同会话的帧混排比较
    strict = all(
        all(p.frames[i].playout_us > p.frames[i - 1].playout_us
            for i in range(1, len(p.frames)))
        for p in result.planners.values()
    )
    drop_counts: dict[str, int] = {r.value: 0 for r in DropReason}
    for d in result.drops:
        drop_counts[d.reason.value] += 1
    peak = max((p.peak_occupancy for p in result.planners.values()), default=0)
    jit = max((p.clock.jitter_us for p in result.planners.values()), default=0)
    # 漂移比与暂停记录只在单会话场景有明确含义
    single = next(iter(result.planners.values()), None)
    ratio = single.clock.clock_ratio if single and len(result.planners) == 1 else 1.0
    pauses = single.sender_pauses if single and len(result.planners) == 1 else []
    return ArmStats(
        name=name, frames=len(frames),
        audio=sum(not f.is_gap for f in frames),
        gaps=sum(f.is_gap for f in frames),
        drops=drop_counts, peak_occupancy=peak,
        final_jitter_us=jit, final_ratio=ratio,
        target_delays=delays, playout_strict_monotonic=strict,
        sender_pauses=pauses)


def run_scenario(
    name: str,
    description: str,
    arrivals: list[RawArrival],
    *,
    expected_ratio: float | None = None,
    fixed_delay_us: int = 40_000,
    assertions: Callable[[dict[str, ArmStats]], list[tuple[str, bool, str]]] | None = None,
    config_overrides: dict | None = None,
    payload_check: bool = True,
    notes: list[str] | None = None,
) -> ScenarioReport:
    overrides = config_overrides or {}
    cfg_adapt = PlannerConfig(**{**overrides, "adaptive": True})
    cfg_fixed = PlannerConfig(**{**overrides, "adaptive": False,
                                 "fixed_delay_us": fixed_delay_us})

    res_adapt = simulate(arrivals, cfg_adapt)
    res_fixed = simulate(arrivals, cfg_fixed)

    hints = OracleHints(
        clock_rate=overrides.get("clock_rate", 8000),
        samples_per_packet=overrides.get("samples_per_packet", 160),
        expected_clock_ratio=expected_ratio, check_payload=payload_check)

    ora_adapt = run_oracle(arrivals, res_adapt.planners,
                           res_adapt.parse_errors, hints)
    hints_fixed = OracleHints(
        clock_rate=hints.clock_rate, samples_per_packet=hints.samples_per_packet,
        expected_clock_ratio=None, check_payload=payload_check)
    ora_fixed = run_oracle(arrivals, res_fixed.planners,
                           res_fixed.parse_errors, hints_fixed)

    arms = {
        "adaptive": _stats("adaptive", res_adapt, False),
        "fixed": _stats("fixed", res_fixed, True),
    }

    assertion_rows: list[dict] = []
    assertion_rows.append({
        "id": "oracle.adaptive", "status": "fail" if not ora_adapt.passed else (
            "uncertain" if ora_adapt.has_uncertain else "pass"),
        "detail": "oracle 全部通过" if ora_adapt.passed
        else "；".join(c.detail for c in ora_adapt.checks if c.status == "fail"),
    })
    assertion_rows.append({
        "id": "oracle.fixed", "status": "pass" if ora_fixed.passed else "fail",
        "detail": "固定基线 oracle 全部通过" if ora_fixed.passed
        else "；".join(c.detail for c in ora_fixed.checks if c.status == "fail"),
    })
    for arm_name, st in arms.items():
        assertion_rows.append({
            "id": f"monotonic.{arm_name}",
            "status": "pass" if st.playout_strict_monotonic else "fail",
            "detail": f"{arm_name} 播放时间严格单调",
        })
    if assertions is not None:
        for aid, ok, detail in assertions(arms):
            assertion_rows.append({
                "id": aid, "status": "pass" if ok else "fail", "detail": detail})

    return ScenarioReport(
        scenario=name, description=description, arms=arms,
        oracle_checks={"adaptive": ora_adapt.to_dicts(),
                       "fixed": ora_fixed.to_dicts()},
        arrivals=len(arrivals), config={
            "min_delay_us": cfg_adapt.min_delay_us,
            "max_delay_us": cfg_adapt.max_delay_us,
            "jitter_multiplier": cfg_adapt.jitter_multiplier,
            "fixed_delay_us": fixed_delay_us,
            **overrides,
        },
        assertions=assertion_rows, notes=notes or [])


# ----------------------------------------------------------------------- #
# 内置场景
# ----------------------------------------------------------------------- #


def scenario_burst_reorder() -> ScenarioReport:
    """突发乱序：每话峰早段有窗口被整体延后 120ms。

    自适应在冷启动话峰必然付出代价（无前序观测，目标延迟只有下限），
    但迟到包的排队峰值被学习，后续话峰目标延迟抬升到 ~125ms，从而 0 缺失；
    40ms 固定基线不学习，每个话峰都丢 1 个。
    """
    hold_us = 120_000
    spec = StreamSpec(
        packets=300, spurt_length=75, jitter_us=3_000,
        burst_holds=[(s, 10, hold_us) for s in range(4)],
        seed=11)
    stream = build_stream(spec)

    def check(arms):
        a, f = arms["adaptive"], arms["fixed"]
        yield (
            "burst.cold_start_pays",
            a.drops["late_after_playout"] == 1 and a.gaps == 1,
            f"自适应冷启动话峰晚到={a.drops['late_after_playout']} "
            f"空缺={a.gaps}（无前序观测，必然付出代价）",
        )
        yield (
            "burst.adaptive_learns",
            f.drops["late_after_playout"] >= 3
            and a.drops["late_after_playout"] <= 1,
            f"固定基线每话峰都丢：晚到={f.drops['late_after_playout']}；"
            f"自适应学习后仅冷启动 {a.drops['late_after_playout']} 次",
        )
        yield (
            "burst.target_delay_rose",
            max(a.target_delays) >= 100_000,
            f"自适应目标延迟峰值 {max(a.target_delays)}us（学到 120ms 突发）",
        )

    return run_scenario(
        "burst_reorder",
        "4 个话峰，每话峰早段有窗口被整体延后 120ms（突发乱序）",
        stream.arrivals, assertions=check, fixed_delay_us=40_000,
        notes=["目标延迟 = clamp(max(k*J, 排队峰值, 跨话峰记忆) + 漂移裕度, 20ms, 400ms)。",
               "迟到包仍参与排队峰值统计，否则系统永远学不到导致丢包的那次突发。"])


def scenario_clock_drift() -> ScenarioReport:
    """时钟漂移：发送端快 2%，自适应帧长跟随漂移；固定静态网格长话峰内丢位置。"""
    spec = StreamSpec(
        packets=600, spurt_length=200, clock_ratio=1.02, seed=23)
    stream = build_stream(spec)

    def check(arms):
        a, f = arms["adaptive"], arms["fixed"]
        yield (
            "drift.ratio_learned",
            abs(a.final_ratio - 1.02) <= 0.004,
            f"自适应最终漂移比 {a.final_ratio:.5f}（真值 1.02）",
        )
        yield (
            "drift.adaptive_no_loss",
            a.drops["late_after_playout"] == 0 and a.gaps == 0,
            f"自适应 晚到={a.drops['late_after_playout']} 空缺={a.gaps}",
        )
        fixed_missing = f.drops["late_after_playout"] + f.gaps
        yield (
            "drift.fixed_worse",
            fixed_missing >= 100,
            f"固定静态网格长话峰内漂移累积，缺失={fixed_missing}（期望上百），"
            f"自适应=0",
        )

    return run_scenario(
        "clock_drift",
        "发送端时钟快 2%（ratio=1.02），3 个 200 包长话峰，逐包无随机抖动",
        stream.arrivals, expected_ratio=1.02, assertions=check,
        fixed_delay_us=40_000,
        notes=["固定基线是静态播放网格：帧长恒为标称值，40ms 余量在约 2s 后被"
               "2% 漂移耗尽，之后每包都错过期限；自适应帧长×1.02 且逐包跟随。"])


def scenario_wraparound() -> ScenarioReport:
    """序号与时间戳双回绕：从边界附近起步，自然跨过 0。"""
    spec = StreamSpec(
        packets=80, start_seq=65530, start_ts=0xFFFFFF00,
        spurt_length=None, seed=31)
    stream = build_stream(spec)

    def check(arms):
        for arm, st in arms.items():
            yield (
                f"wrap.contents.{arm}",
                st.audio == 80 and st.gaps == 0,
                f"{arm} 音频={st.audio} 空缺={st.gaps}（期望 80/0）",
            )

    rep = run_scenario(
        "wraparound",
        "序号从 65530、时间戳从 0xFFFFFF00 起步，80 包内各自回绕",
        stream.arrivals, assertions=check, fixed_delay_us=40_000,
        notes=["16 位序号与 32 位时间戳由独立展开器按各自位宽展开。"])
    # 规划器内部回绕计数：单独再跑一遍直接取证
    res = simulate(stream.arrivals, PlannerConfig(adaptive=True))
    p = next(iter(res.planners.values()))
    rep.notes.append(
        f"展开器取证：序号正向回绕 {p.seq_unwraps} 次，"
        f"时间戳正向回绕 {p.ts_unwraps} 次")
    rep.assertions.append({
        "id": "wrap.events",
        "status": "pass" if p.seq_unwraps >= 1 and p.ts_unwraps >= 1 else "fail",
        "detail": f"seq wraps={p.seq_unwraps}, ts wraps={p.ts_unwraps}",
    })
    return rep


def scenario_pause_resume() -> ScenarioReport:
    """暂停 1.5s 后重启：时间轴跳过，播放单调；不合成“补帧音频”。

    暂停时长刻意超过缓冲可吸收量（目标延迟+残余排队），因此暂停尾部必然
    产出一串显式空缺；恢复后音频照常，且空缺不被“补帧音频”掩盖。
    """
    spec = StreamSpec(
        packets=200, spurt_length=None, seed=41,
        pauses=[(99, 1_500_000)])
    stream = build_stream(spec)

    def check(arms):
        a = arms["adaptive"]
        # 1.5s = 75 个 20ms 静默帧；发送端整段不发包，全部位置显式空缺
        yield (
            "pause.gaps_explicit",
            a.gaps == 75,
            f"暂停段空缺 {a.gaps} 个（期望 75 = 1.5s/20ms，全部显式标记）",
        )
        yield (
            "pause.audio_after_resume",
            a.audio == 200,
            f"真实音频帧 {a.audio}（期望 200，不允许合成音频充数）",
        )
        yield (
            "pause.explained",
            len(a.sender_pauses) == 1
            and a.sender_pauses[0]["skipped_sequence_numbers"] == 75
            and abs(a.sender_pauses[0]["silence_ms"] - 1500.0) < 1.0,
            f"发送端静默记录 {a.sender_pauses}",
        )

    return run_scenario(
        "pause_resume",
        "200 包，第 100 包后发送端停发 1.5s（序号与时间戳一并跳过静默帧）",
        stream.arrivals, assertions=check, fixed_delay_us=40_000,
        notes=["暂停前的排队内容会被播放时钟自然消耗；缓冲耗尽后的空缺显式标记。",
               "恢复包揭示的发送端静默在 sender_pauses 中单列解释；历史空缺不回溯改写。"])


def scenario_duplicates() -> ScenarioReport:
    spec = StreamSpec(
        packets=120, spurt_length=60, seed=53,
        duplicates=[(10, 5_000), (70, 12_000), (110, 50_000)])
    stream = build_stream(spec)

    def check(arms):
        a, f = arms["adaptive"], arms["fixed"]
        yield (
            "dup.adaptive",
            a.drops["duplicate"] == 3,
            f"自适应 duplicate={a.drops['duplicate']}（期望 3）",
        )
        yield (
            "dup.fixed",
            f.drops["duplicate"] == 3,
            f"固定基线 duplicate={f.drops['duplicate']}（期望 3）",
        )
        yield (
            "dup.no_gap_from_dup",
            a.gaps == 0 and a.audio == 120,
            f"重复包不应产生空缺：空缺={a.gaps} 音频={a.audio}",
        )

    return run_scenario(
        "duplicates",
        "120 包中 3 个包被重发（5ms/12ms/50ms 后）",
        stream.arrivals, assertions=check, fixed_delay_us=40_000)


def scenario_overflow() -> ScenarioReport:
    """缓冲硬上界：300 包逆序一次性到达，验证尾丢弃与有界性。"""
    spec = StreamSpec(packets=300, spurt_length=None, jitter_us=0, seed=61)
    built = build_stream(spec)
    arrivals = sorted(built.arrivals,
                      key=lambda a: (-a.arrival_us,))
    # 全部压到同一时刻并逆序
    arrivals = [RawArrival(arrival_us=1_000_000, data=a.data)
                for a in arrivals]

    def check(arms):
        for arm, st in arms.items():
            yield (
                f"overflow.bounded.{arm}",
                st.peak_occupancy <= 250,
                f"{arm} 缓冲峰值 {st.peak_occupancy}（上界 250）",
            )
            yield (
                f"overflow.drops.{arm}",
                st.drops["overflow"] == 50,
                f"{arm} overflow={st.drops['overflow']}（期望 50）",
            )
            total_missing = st.gaps + st.drops["late_after_playout"]
            yield (
                f"overflow.accounted.{arm}",
                st.audio + total_missing == 300,
                f"{arm} 音频 {st.audio} + 缺失 {total_missing} = 300",
            )

    return run_scenario(
        "overflow",
        "300 包在同一时刻逆序到达，max_buffer_packets=250",
        arrivals, assertions=check, fixed_delay_us=40_000,
        notes=["尾丢弃播放时刻最远的包，保护即将播放的 250 包。"])


def scenario_ssrc_switch() -> ScenarioReport:
    specs = [
        StreamSpec(ssrc=0x11111111, packets=80, start_arrival_us=1_000_000,
                   spurt_length=40, seed=71),
        StreamSpec(ssrc=0x22222222, packets=80, start_arrival_us=1_010_000,
                   spurt_length=40, jitter_us=2_000, seed=72),
    ]
    arrivals = ssrc_switch_stream(specs)

    def check(arms):
        for arm, st in arms.items():
            yield (
                f"ssrc.{arm}",
                st.audio == 160 and st.gaps == 0,
                f"{arm} 两个会话合计音频={st.audio} 空缺={st.gaps}",
            )

    rep = run_scenario(
        "ssrc_switch",
        "两个 SSRC 交错到达（同序号空间重叠），必须各自独立成会话",
        arrivals, assertions=check, fixed_delay_us=40_000)
    res = simulate(arrivals, PlannerConfig(adaptive=True))
    rep.assertions.append({
        "id": "ssrc.two_sessions",
        "status": "pass" if len(res.planners) == 2 else "fail",
        "detail": f"会话数={len(res.planners)}: {sorted(res.planners)}",
    })
    return rep


SCENARIOS = {
    "burst_reorder": scenario_burst_reorder,
    "clock_drift": scenario_clock_drift,
    "wraparound": scenario_wraparound,
    "pause_resume": scenario_pause_resume,
    "duplicates": scenario_duplicates,
    "overflow": scenario_overflow,
    "ssrc_switch": scenario_ssrc_switch,
}


def run_all() -> list[ScenarioReport]:
    return [fn() for fn in SCENARIOS.values()]
