"""引擎外观：文本规范 → 计划 → 执行，统一输出结果、统计、失败与不确定性。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from ..storage.version_store import StorageError, VersionStore
from ..text import spec
from . import planner as PL
from .executor import Executor, StepStats


# 机器可读失败类别（与 HTTP 状态解耦）
CAT_SPEC = "spec_error"
CAT_VALIDATION = "validation_error"
CAT_VERSION_NOT_FOUND = "version_not_found"
CAT_STORAGE = "storage_error"
CAT_INTERNAL = "internal_error"
CAT_ORDER = "order_error"


@dataclass
class QueryOutcome:
    ok: bool
    request_id: str
    expression: str
    version: int | None
    order: str
    result: list[int] = field(default_factory=list)
    count: int = 0
    stats: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    ast: dict | None = None
    warnings: list[str] = field(default_factory=list)
    short_circuited: bool = False
    skipped_nodes: list[str] = field(default_factory=list)
    error_category: str | None = None
    error_message: str | None = None
    error_position: int | None = None
    uncertainty: list[str] = field(default_factory=list)

    def to_response(self) -> dict[str, Any]:
        if self.ok:
            return {
                "ok": True,
                "request_id": self.request_id,
                "expression": self.expression,
                "version": self.version,
                "order": self.order,
                "result": self.result,
                "count": self.count,
                "short_circuited": self.short_circuited,
                "skipped_nodes": self.skipped_nodes,
                "stats": self.stats,
                "warnings": self.warnings,
                "uncertainty": self.uncertainty,
                "steps": self.steps,
                "ast": self.ast,
            }
        return {
            "ok": False,
            "request_id": self.request_id,
            "expression": self.expression,
            "version": self.version,
            "order": self.order,
            "error": {
                "category": self.error_category,
                "message": self.error_message,
                "position": self.error_position,
            },
            "uncertainty": self.uncertainty,
        }


class QueryEngine:
    def __init__(self, store: VersionStore) -> None:
        self.store = store

    # ------------------------------------------------------------------

    def _build_plan(
        self, expression: str, version: int | None
    ) -> tuple[PL.Plan, int]:
        # 1) 文本规范解析（失败类别 spec_error，带字符位置）
        ast = spec.parse_query(expression)
        # 2) 版本解析（空全集版本 0 合法；显式不存在版本 -> version_not_found）
        resolved = self.store.resolve_version(version)
        # 3) 绑定存储
        plan = PL.Planner(self.store, resolved, expression).build()
        return plan, resolved

    def query(
        self,
        expression: str,
        *,
        version: int | None = None,
        order: str = PL.ORDER_RARE_FIRST,
        request_id: str = "-",
    ) -> QueryOutcome:
        outcome = QueryOutcome(
            ok=False,
            request_id=request_id,
            expression=expression,
            version=version,
            order=order,
        )
        try:
            if order not in PL.ALL_ORDERS:
                outcome.error_category = CAT_ORDER
                outcome.error_message = (
                    f"未知执行顺序 {order!r}，可选：{', '.join(PL.ALL_ORDERS)}"
                )
                return outcome
            plan, resolved = self._build_plan(expression, version)
            outcome.version = resolved
            outcome.ast = spec.to_dict(spec.parse_query(expression))
            outcome.warnings = list(plan.warnings)
            outcome.uncertainty = list(plan.warnings)
            exec_result = Executor(self.store, resolved, order).execute(plan)
            outcome.ok = True
            outcome.result = exec_result.ids
            outcome.count = len(exec_result.ids)
            outcome.steps = [s.to_dict() for s in exec_result.steps]
            outcome.stats = asdict(exec_result.stats)
            outcome.short_circuited = exec_result.short_circuited
            outcome.skipped_nodes = exec_result.skipped_nodes
            return outcome
        except spec.SpecError as e:
            outcome.error_category = CAT_SPEC
            outcome.error_message = e.message
            outcome.error_position = e.position
            return outcome
        except PL.QueryValidationError as e:
            outcome.error_category = CAT_VALIDATION
            outcome.error_message = str(e)
            return outcome
        except StorageError as e:
            msg = str(e)
            outcome.error_category = (
                CAT_VERSION_NOT_FOUND if "版本" in msg and "不存在" in msg else CAT_STORAGE
            )
            outcome.error_message = msg
            return outcome
        except Exception as e:  # 最后一道防线：内部错误也要可解释
            outcome.error_category = CAT_INTERNAL
            outcome.error_message = f"{type(e).__name__}: {e}"
            return outcome

    def explain(
        self,
        expression: str,
        *,
        version: int | None = None,
        request_id: str = "-",
    ) -> QueryOutcome | None:
        """用三种执行顺序各跑一次：结果必须逐 ID 一致；统计可能不同。"""
        per_order: list[dict] = {}
        base: QueryOutcome | None = None
        for order in PL.ALL_ORDERS:
            o = self.query(
                expression, version=version, order=order, request_id=request_id
            )
            if not o.ok:
                return o
            per_order[order] = {
                "result": o.result,
                "count": o.count,
                "short_circuited": o.short_circuited,
                "skipped_nodes": o.skipped_nodes,
                "stats": o.stats,
                "steps": o.steps,
            }
            if base is None:
                base = o
        reference = per_order[PL.ORDER_RARE_FIRST]["result"]
        consistent = all(
            per_order[k]["result"] == reference for k in PL.ALL_ORDERS
        )
        assert base is not None
        if not consistent:
            base.ok = False
            base.error_category = CAT_INTERNAL
            base.error_message = "不同执行顺序结果不一致——内部不变量被破坏"
            return base
        explain_view = {
            "consistent_across_orders": consistent,
            "orders": per_order,
            "block_skip_summary": {
                k: per_order[k]["stats"]["blocks_skipped"] for k in PL.ALL_ORDERS
            },
        }
        return _ExplainOutcome(base, explain_view)

    # ------------------------------------------------------------------


@dataclass
class _ExplainOutcome:
    """explain 的轻量包装：to_response 与 QueryOutcome 兼容。"""

    base: QueryOutcome
    explain: dict

    def __getattr__(self, name: str):
        # 未在包装器上定义的属性（version/count/steps/stats/...）全部委托给 base
        return getattr(self.base, name)

    @property
    def ok(self) -> bool:
        return self.base.ok

    @property
    def request_id(self) -> str:
        return self.base.request_id

    def to_response(self) -> dict[str, Any]:
        resp = self.base.to_response()
        resp["explain"] = self.explain
        return resp
