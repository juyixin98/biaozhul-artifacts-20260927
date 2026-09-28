"""提交前验证：计划资源上限检查。

决策（planner）完成后、事务开启前执行。这里的失败全部归类为
RESOURCE_EXHAUSTED（或发现不可序列化内容时的 COMPUTATION_FAILURE），
与输入错误和状态冲突区分开。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .adapter import canonical_size
from .config import MergeSpec
from .contracts import MergePlan
from .errors import ComputationFailure, PlanTooLargeError


@dataclass(frozen=True)
class PlanStats:
    source_rows: int
    write_actions: int
    plan_bytes: int
    limits: dict[str, int]


def check_source_limits(source_row_count: int, source_bytes: int,
                        spec: MergeSpec) -> None:
    """适配后即可执行的早期检查（在决策之前）。"""
    if source_row_count > spec.max_source_rows:
        raise PlanTooLargeError(
            f"source has {source_row_count} rows, limit is {spec.max_source_rows}",
            details={"rows": source_row_count, "limit": spec.max_source_rows,
                     "limit_name": "max_source_rows"},
        )
    if source_bytes > spec.max_plan_bytes:
        raise PlanTooLargeError(
            f"source payload is {source_bytes} bytes, limit is {spec.max_plan_bytes}",
            details={"bytes": source_bytes, "limit": spec.max_plan_bytes,
                     "limit_name": "max_plan_bytes"},
        )


def validate_plan(plan: MergePlan, spec: MergeSpec) -> PlanStats:
    writes = plan.write_actions()
    plan_bytes = _plan_bytes(plan)

    if len(writes) > spec.max_actions:
        raise PlanTooLargeError(
            f"plan has {len(writes)} write actions, limit is {spec.max_actions}",
            details={"write_actions": len(writes), "limit": spec.max_actions,
                     "limit_name": "max_actions"},
        )
    if plan_bytes > spec.max_plan_bytes:
        raise PlanTooLargeError(
            f"serialized plan is {plan_bytes} bytes, limit is {spec.max_plan_bytes}",
            details={"bytes": plan_bytes, "limit": spec.max_plan_bytes,
                     "limit_name": "max_plan_bytes"},
        )

    stats = PlanStats(
        source_rows=sum(1 for a in plan.actions if a.source_rownum is not None),
        write_actions=len(writes),
        plan_bytes=plan_bytes,
        limits={
            "max_source_rows": spec.max_source_rows,
            "max_actions": spec.max_actions,
            "max_plan_bytes": spec.max_plan_bytes,
        },
    )
    return stats


def _plan_bytes(plan: MergePlan) -> int:
    total = 0
    for action in plan.actions:
        total += canonical_size(action.to_dict())
    return total


def assert_json_serializable(plan: MergePlan) -> None:
    """防御性检查：计划必须能完整序列化进日志/API。"""
    try:
        json.dumps(plan.to_dict())
    except (TypeError, ValueError) as exc:
        raise ComputationFailure(
            f"plan is not JSON serializable: {exc}",
        ) from exc
