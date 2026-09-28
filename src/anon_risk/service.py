"""服务编排层：解析 → 层级校验 → 指标/优化 → 安全视图，并贯穿审计与日志关联。

所有跨模块流程集中在这里；HTTP 层只做鉴权与序列化，内核不接触 I/O，
存储不接触指标计算。
"""

from __future__ import annotations

from typing import Any, Optional

from .errors import ErrorCode, RiskError
from .kernel import equivalence as eq
from .kernel import optimizer as opt
from .kernel.hierarchy import apply_vector, materialize
from .kernel.parser import parse_dataset
from .kernel.types import Dataset
from .logging_setup import get_logger, log_context
from .security.view import report_view, suggestion_view
from .storage import AuditLog, RunStore

log = get_logger("service")


class RunService:
    def __init__(self, settings, key_manager, store: RunStore, audit: AuditLog):
        self.settings = settings
        self.keys = key_manager
        self.store = store
        self.audit = audit
        self.metric_version = settings.app.metric_version
        self.combo_cap = settings.kernel.lattice_combo_cap
        self.risk_medium_factor = settings.kernel.risk_medium_factor

    # ---- 内部工具 ----------------------------------------------------
    def _prepare(self, dataset_payload: dict[str, Any]) -> tuple[Dataset, dict]:
        """解析 + 层级实例化（包含关系校验），返回数据与校验证据。"""
        dataset = parse_dataset(dataset_payload)
        materialized = {}
        validations = []
        for col in dataset.qi_columns:
            mh = materialize(
                dataset.hierarchies[col],
                [row[col] for row in dataset.rows],
            )
            materialized[col] = mh
            validations.append(mh.validation)
        return dataset, {"materialized": materialized,
                         "validations": validations}

    def _normalize_levels(self, dataset: Dataset,
                          requested: Optional[dict[str, int]]) -> dict[str, int]:
        levels = {c: 0 for c in dataset.qi_columns}
        if requested:
            unknown = sorted(set(requested) - set(levels))
            if unknown:
                raise RiskError(
                    f"levels 含未声明的准标识符列 {unknown}",
                    code=ErrorCode.COLUMN_NOT_FOUND,
                    details={"columns": unknown},
                )
            for c, v in requested.items():
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise RiskError(
                        f"列 {c} 的泛化层级必须是非负整数",
                        code=ErrorCode.INVALID_PARAMETER,
                        details={"column": c, "got": str(v)},
                    )
                if v > dataset.hierarchies[c].depth:
                    raise RiskError(
                        f"列 {c} 请求第 {v} 级，但层级深度只有 "
                        f"{dataset.hierarchies[c].depth}",
                        code=ErrorCode.HIERARCHY_LEVEL_NOT_FOUND,
                        details={"column": c, "requested": v,
                                 "max": dataset.hierarchies[c].depth},
                    )
                levels[c] = v
        return levels

    # ---- API 流程 ----------------------------------------------------
    def create_run(self, payload: dict[str, Any], *,
                   correlation_id: str, actor: str = "anonymous") -> dict:
        # 先做解析与层级校验，失败也写审计（VALIDATION_ERROR），不落运行文件
        try:
            dataset, prepared = self._prepare(payload)
        except RiskError as exc:
            self.audit.append(
                event="run_create", status="VALIDATION_ERROR", actor=actor,
                correlation_id=correlation_id,
                error_code=exc.code.value,
                metric_version=self.metric_version,
                details={"error_message": exc.message},
            )
            raise

        rec = self.store.create_run(payload, metric_version=self.metric_version)
        with log_context(run_id=rec.run_id, correlation_id=correlation_id):
            self.audit.append(
                event="run_create", status="SUCCESS", actor=actor,
                run_id=rec.run_id, correlation_id=correlation_id,
                metric_version=self.metric_version,
                details={"rows": rec.row_count,
                         "qi_columns": rec.qi_columns,
                         "sensitive_columns": rec.sensitive_columns},
            )
        return {
            "run_id": rec.run_id,
            "access_token": rec.token,
            "created_at": rec.created_at,
            "row_count": rec.row_count,
            "quasi_identifiers": rec.qi_columns,
            "sensitive": rec.sensitive_columns,
            "null_counts": dataset.null_counts,
            "hierarchy_validation": prepared["validations"],
            "metric_version": self.metric_version,
        }

    def _open(self, run_id: str, token: str):
        return self.store.open_run(run_id, token)

    def evaluate(self, run_id: str, token: str, requested: Optional[dict[str, int]],
                 k: int, l: int, *, correlation_id: str,
                 actor: str = "anonymous") -> dict:
        with log_context(run_id=run_id, correlation_id=correlation_id):
            conn, meta, payload = self._open(run_id, token)
            try:
                dataset, prepared = self._prepare(payload)
                levels = self._normalize_levels(dataset, requested)
                keys = apply_vector(
                    dataset.rows, dataset.qi_columns,
                    prepared["materialized"], levels,
                )
                report = eq.evaluate(
                    dataset, keys, levels, k, l,
                    risk_medium_factor=self.risk_medium_factor,
                    metric_version=self.metric_version,
                )
                hmac = self.keys.hmac_for_run(run_id)
                view = report_view(report, hmac)
                self.store.record_operation(conn, "evaluate", "SUCCESS", {
                    "k": k, "l": l, "levels": levels,
                    "k_ok": report.k_ok, "l_ok": report.l_ok,
                    "classes": len(report.classes),
                    "dm": report.discernibility,
                })
                self.audit.append(
                    event="evaluate", status="SUCCESS", actor=actor,
                    run_id=run_id, correlation_id=correlation_id,
                    metric_version=self.metric_version,
                    details={"k": k, "l": l, "levels": levels,
                             "k_ok": report.k_ok, "l_ok": report.l_ok,
                             "classes": len(report.classes)},
                )
                return view
            finally:
                conn.close()

    def suggest(self, run_id: str, token: str, k: int, l: int, *,
                correlation_id: str, actor: str = "anonymous") -> dict:
        with log_context(run_id=run_id, correlation_id=correlation_id):
            conn, _meta, payload = self._open(run_id, token)
            try:
                dataset, prepared = self._prepare(payload)
                result = opt.suggest(
                    dataset, prepared["materialized"], k, l,
                    combo_cap=self.combo_cap,
                    risk_medium_factor=self.risk_medium_factor,
                    metric_version=self.metric_version,
                )
                view = suggestion_view(result)
                status = "SUCCESS" if result.feasible else "UNREACHABLE"
                self.store.record_operation(conn, "suggest", status, {
                    "k": k, "l": l,
                    "feasible": result.feasible,
                    "levels": result.levels if result.feasible else None,
                    "dm": result.discernibility,
                    "evaluated_vectors": result.evaluated_vectors,
                })
                self.audit.append(
                    event="suggest", status=status, actor=actor,
                    run_id=run_id, correlation_id=correlation_id,
                    metric_version=self.metric_version,
                    details={"k": k, "l": l, "feasible": result.feasible,
                             "evaluated_vectors": result.evaluated_vectors,
                             "total_vectors": result.total_vectors,
                             "basis": result.verdict_basis},
                )
                return view
            finally:
                conn.close()

    # ---- 运行管理 / 审计 --------------------------------------------
    def list_runs(self) -> list[dict]:
        return self.store.list_runs()

    def get_run(self, run_id: str) -> dict:
        return self.store.get_summary(run_id)

    def operations(self, run_id: str, token: str) -> dict:
        return self.store.list_operations(run_id, token)

    def delete_run(self, run_id: str, token: str, *,
                   correlation_id: str, actor: str = "anonymous") -> None:
        with log_context(run_id=run_id, correlation_id=correlation_id):
            self.store.delete_run(run_id, token)
            self.audit.append(
                event="run_delete", status="SUCCESS", actor=actor,
                run_id=run_id, correlation_id=correlation_id,
                metric_version=self.metric_version,
            )

    def audit_events(self, *, run_id: Optional[str], limit: int, offset: int,
                     status: Optional[str], correlation_id: str) -> dict:
        return self.audit.query(run_id=run_id, limit=limit, offset=offset,
                                status=status)
