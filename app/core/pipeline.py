"""请求处理管线：把“一批报文或夹具名”变成可解释的仿真结果。

两种输入模式：

- ``scenario``：指定内置夹具名（如 burst_reorder / clock_drift ...）；
- ``raw``：直接提交 RTP 报文（base64）+ 到达时刻，用给定规划参数仿真。

输出统一包含：双臂（自适应/固定）计划统计、oracle 复核、丢弃原因分布、
不确定性结论。失败与不确定项单列，不与正常结果混淆。
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from app.config import PlannerConfig, config_snapshot
from app.core.models import DropReason
from app.core.oracle import OracleHints, run_oracle
from app.core.scenarios import SCENARIOS
from app.core.simulator import RawArrival, simulate

# 结果里最多回显多少条计划帧，避免响应无限大
MAX_PLAN_FRAMES = 500


class PipelineError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _frame_brief(f) -> dict[str, Any]:
    return {
        "seq": f.seq,
        "ssrc": f.ssrc,
        "kind": f.kind.value,
        "rtp_ts": f.rtp_ts,
        "playout_us": f.playout_us,
        "arrival_us": f.arrival_us,
        "gap_reason": f.gap_reason.value if f.gap_reason else None,
        "talkspurt_id": f.talkspurt_id,
        "drift_ratio": round(f.drift_ratio, 6),
        "target_delay_us": f.target_delay_us,
    }


def _drop_brief(d) -> dict[str, Any]:
    return {
        "reason": d.reason.value,
        "ssrc": d.ssrc,
        "seq": d.seq,
        "arrival_us": d.arrival_us,
        "detail": d.detail,
    }


def _arm_summary(name: str, result) -> dict[str, Any]:
    planner_objs = list(result.planners.values())
    frames = result.frames
    playouts_by_ssrc = {
        ssrc: [f.playout_us for f in p.frames]
        for ssrc, p in result.planners.items()
    }
    monotonic = all(
        all(v[i] > v[i - 1] for i in range(1, len(v)))
        for v in playouts_by_ssrc.values()
    )
    drop_counts = {r.value: 0 for r in DropReason}
    for d in result.drops:
        drop_counts[d.reason.value] += 1

    ssrc_stats = {}
    for ssrc, p in result.planners.items():
        ssrc_stats[str(ssrc)] = {
            "frames": len(p.frames),
            "audio": sum(not f.is_gap for f in p.frames),
            "gaps": sum(f.is_gap for f in p.frames),
            "peak_occupancy": p.peak_occupancy,
            "jitter_us": p.clock.jitter_us,
            "queue_spike_us": p.clock.spike_us,
            "clock_ratio": round(p.clock.clock_ratio, 6),
            "seq_unwraps": p.seq_unwraps,
            "ts_unwraps": p.ts_unwraps,
            "talkspurts": p.talkspurt_count,
            "sender_pauses": p.sender_pauses,
        }

    return {
        "arm": name,
        "totals": {
            "frames": len(frames),
            "audio": sum(not f.is_gap for f in frames),
            "gaps": sum(f.is_gap for f in frames),
            "drops": drop_counts,
            "sessions": len(result.planners),
            "strict_playout_monotonic": monotonic,
        },
        "per_ssrc": ssrc_stats,
        "plan": [_frame_brief(f) for f in frames[:MAX_PLAN_FRAMES]],
        "plan_truncated": len(frames) > MAX_PLAN_FRAMES,
        "drops_detail": [_drop_brief(d) for d in result.drops[:200]],
    }


def run_scenario_named(name: str,
                       fixed_delay_us: Optional[int] = None) -> dict[str, Any]:
    if name not in SCENARIOS:
        raise PipelineError(
            "unknown_scenario",
            f"未知场景 {name!r}，可选: {sorted(SCENARIOS)}")
    # 场景自带断言/notes；直接复用场景报告
    report = SCENARIOS[name]()
    return report.to_dict()


def _decode_packets(packets: list[dict[str, Any]]) -> list[RawArrival]:
    arrivals: list[RawArrival] = []
    for i, item in enumerate(packets):
        try:
            arrival_us = int(item["arrival_us"])
            encoding = item.get("encoding", "base64")
            if encoding == "base64":
                data = base64.b64decode(item["rtp_base64"])
            elif encoding == "hex":
                data = bytes.fromhex(item["rtp_hex"])
            else:
                raise PipelineError("bad_encoding", f"#{i}: {encoding}")
        except KeyError as exc:
            raise PipelineError("missing_field", f"#{i}: 缺字段 {exc}") from exc
        except (ValueError, TypeError) as exc:
            if isinstance(exc, PipelineError):
                raise
            raise PipelineError("bad_packet", f"#{i}: {exc}") from exc
        arrivals.append(RawArrival(arrival_us=arrival_us, data=data))
    return arrivals


def run_raw_packets(packets: list[dict[str, Any]],
                    params: Optional[dict[str, Any]] = None,
                    expected_clock_ratio: Optional[float] = None,
                    fixed_delay_us: int = 40_000) -> dict[str, Any]:
    params = params or {}
    arrivals = _decode_packets(packets)
    allowed = {f.name for f in PlannerConfig.__dataclass_fields__.values()}
    overrides = {k: v for k, v in params.items() if k in allowed}
    overrides.pop("adaptive", None)
    cfg_adapt = PlannerConfig(**overrides, adaptive=True)
    cfg_fixed = PlannerConfig(
        **overrides, adaptive=False,
        fixed_delay_us=int(overrides.get("fixed_delay_us", fixed_delay_us)))

    res_adapt = simulate(arrivals, cfg_adapt)
    res_fixed = simulate(arrivals, cfg_fixed)

    clock_rate = overrides.get("clock_rate", 8000)
    spp = overrides.get("samples_per_packet", 160)
    hints = OracleHints(
        clock_rate=clock_rate, samples_per_packet=spp,
        expected_clock_ratio=expected_clock_ratio,
        check_payload=False)  # 外部原始报文负载非本系统合成，不做内容比对
    ora = run_oracle(arrivals, res_adapt.planners, res_adapt.parse_errors, hints)

    return {
        "mode": "raw",
        "arrivals": len(arrivals),
        "config": config_snapshot(cfg_adapt),
        "adaptive": _arm_summary("adaptive", res_adapt),
        "fixed": _arm_summary("fixed", res_fixed),
        "oracle_checks": ora.to_dicts(),
        "oracle_passed": ora.passed,
        "uncertain": [c.to_dict() for c in ora.checks if c.status == "uncertain"],
        "parse_errors": [_drop_brief(d) for d in res_adapt.parse_errors],
    }
