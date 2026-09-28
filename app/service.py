"""编排层：解析 → 安全内核 → 状态持久化 → 审计，串起真实输入到输出。"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.core.anonymization import ClassInfo, find_best_generalization
from app.core.errors import CATEGORY, FailureCode, ServiceError
from app.core.logging_setup import (
    StepLogger,
    bind_run,
    configure_logging,
    get_logger,
    log_event,
    new_run_id,
)
from app.core.parsing import Dataset, parse_dataset
from app.models import (
    DatasetIn,
    EquivalenceClassOut,
    LevelsOut,
    RunResponse,
    RunSummary,
)
from app.security.audit import AuditLog
from app.security.crypto import CryptoBox, fingerprint
from app.store.db import Store

INFO_LOSS_METRIC = (
    "mean per-row group-expansion: avg over rows and quasi-identifiers of "
    "(group_size-1)/(domain_size-1) in [0,1]"
)


def _risk_level(c: ClassInfo, k: int, l: int) -> str:
    if c.size < k:
        return "high"  # k 违规：可能唯一识别
    if c.distinct_sensitive < l:
        return "medium"  # k 满足但敏感属性同质
    return "low"


def _class_out(c: ClassInfo, k: int, l: int) -> EquivalenceClassOut:
    return EquivalenceClassOut(
        class_index=c.class_index,
        size=c.size,
        distinct_sensitive=c.distinct_sensitive,
        max_sensitive_frequency=c.max_sensitive_frequency,
        meets_k=c.size >= k,
        meets_l=c.size >= k and c.distinct_sensitive >= l,
        risk_level=_risk_level(c, k, l),
        contains_null_qi=c.contains_null_qi,
    )


def _summary(classes: list[ClassInfo], ds: Dataset) -> RunSummary:
    n = len(ds.rows)
    below_k = [c for c in classes if c.size < ds.k]
    below_l = [c for c in classes if c.size >= ds.k and c.distinct_sensitive < ds.l]
    rows_below = sum(c.size for c in below_k)
    null_rows = sum(ds.null_row_flags)
    return RunSummary(
        n_rows=n,
        n_classes=len(classes),
        min_class_size=min(c.size for c in classes),
        max_class_size=max(c.size for c in classes),
        classes_below_k=len(below_k),
        classes_below_l=len(below_l),
        rows_in_below_k_classes=rows_below,
        rows_with_any_null_qi=null_rows,
        fraction_identifiable=round(rows_below / n, 6) if n else 0.0,
    )


class RiskService:
    def __init__(
        self, settings: Settings, store: Store, crypto: CryptoBox, audit: AuditLog
    ) -> None:
        self.settings = settings
        self.store = store
        self.crypto = crypto
        self.audit = audit
        # 按本实例配置装配日志（测试隔离；重复调用会安全关闭旧 handler）
        configure_logging(settings.app_log_path or None)
        self.log = get_logger("service")

    # ------------------------------------------------------------------ #
    def submit_dataset(self, payload: DatasetIn) -> dict[str, Any]:
        """解析、构建密文存储，返回 schema 元数据（不含行数据）。"""
        fp = fingerprint(payload.model_dump(mode="json"))
        ds = parse_dataset(payload, self.settings)

        log_event(
            self.log,
            "dataset_submitted",
            name=ds.name,
            input_fingerprint=fp,
            n_rows=len(ds.rows),
            n_qi=len(ds.qi_columns),
            n_sensitive=len(ds.sensitive_columns),
            n_null_qi_rows=sum(ds.null_row_flags),
        )

        schema_id = "sch_" + fp[:24]
        column_roles = [
            {
                "name": c.name,
                "role": c.role.value,
                "height": c.hierarchy.height if c.hierarchy else None,
            }
            for c in ds.columns
        ]
        # 完整列声明（含层级）随密文保存，保证后续可用新阈值重放分析；
        # 层级定义本身不是秘密，但与行数据一起保持"密文静态存储"的一致边界。
        column_specs = [
            c.model_dump(mode="json", exclude_none=True) for c in payload.columns
        ]
        secret = {
            "columns": [
                {"name": c.name, "role": c.role.value} for c in ds.columns
            ],
            "column_specs": column_specs,
            "rows": ds.rows,
            "k": ds.k,
            "l": ds.l,
            "input_fingerprint": fp,
        }
        inserted = self.store.save_schema(
            schema_id,
            ds.name,
            fp,
            column_roles,
            len(ds.rows),
            ds.k,
            ds.l,
            secret,
        )
        self.audit.append(
            "submit_dataset",
            "succeeded",
            run_id=schema_id,
            detail={
                "schema_id": schema_id,
                "input_fingerprint": fp,
                "n_rows": len(ds.rows),
                "n_qi": len(ds.qi_columns),
                "key_source": self.crypto.key_source,
                "outcome": "inserted" if inserted else "existing_fingerprint_reused",
            },
        )
        return {
            "schema_id": schema_id,
            "name": ds.name,
            "input_fingerprint": fp,
            "n_rows": len(ds.rows),
            "column_roles": column_roles,
            "k": ds.k,
            "l": ds.l,
            "inserted": inserted,
            "warnings": ds.warnings,
        }

    # ------------------------------------------------------------------ #
    def analyze(
        self,
        payload: DatasetIn,
        schema_id: str | None = None,
        *,
        persist: bool = True,
    ) -> RunResponse:
        """端到端分析。

        ``persist=False`` 时只返回结果、不写运行表与 analyze 审计
        （供"先提交再分析"内部组合复用，避免重复记账）。
        """
        from app import __version__

        run_id = new_run_id()
        fp = fingerprint(payload.model_dump(mode="json"))
        t1, t2 = bind_run(run_id=run_id, input_fingerprint=fp)
        steps = StepLogger()
        steps.step(
            "run_start",
            "analysis run started",
            basis="explicit quasi_identifier/sensitive declarations; NULL retained",
            run_id=run_id,
            input_fingerprint=fp,
            service_version=__version__,
            k=payload.k,
            l=payload.l,
        )

        try:
            ds = parse_dataset(payload, self.settings)
            for w in ds.warnings:
                steps.step("warning", w, basis="normalization kept NULL/missing in sample")
            result = find_best_generalization(ds, steps)

            n_null_rows = sum(ds.null_row_flags)
            qi_names = [c.name for c in ds.qi_columns]

            if result.status != "succeeded":
                # 明确失败：不可达。输出阻塞类的真实计数，但不回显任何取值。
                blockers_ev = result.top
                classes_out = [
                    _class_out(c, ds.k, ds.l)
                    for c in blockers_ev.classes[: self.settings.max_classes_returned]
                ]
                truncated = len(blockers_ev.classes) > self.settings.max_classes_returned
                resp = RunResponse(
                    run_id=run_id,
                    schema_id=schema_id or "",
                    status=result.status,
                    failure_code=result.failure_code,
                    failure_category=CATEGORY[FailureCode(result.failure_code)],
                    message=result.message,
                    k=ds.k,
                    l=ds.l,
                    chosen_levels=None,
                    info_loss=None,
                    info_loss_metric=None,
                    n_combinations_explored=result.n_combinations_explored,
                    summary=_summary(blockers_ev.classes, ds),
                    equivalence_classes=classes_out,
                    classes_truncated=truncated,
                    null_kept_in_sample=True,
                    n_null_rows=n_null_rows,
                    computation_trace=result.trace,
                    warnings=ds.warnings,
                    service_version=__version__,
                )
                if persist:
                    self._persist_and_audit(schema_id, resp, "failed")
                return resp

            best = result.best
            assert best is not None
            classes_out = [
                _class_out(c, ds.k, ds.l)
                for c in best.classes[: self.settings.max_classes_returned]
            ]
            truncated = len(best.classes) > self.settings.max_classes_returned
            resp = RunResponse(
                run_id=run_id,
                schema_id=schema_id or "",
                status="succeeded",
                failure_code=None,
                failure_category=None,
                message=result.message,
                k=ds.k,
                l=ds.l,
                chosen_levels=[
                    LevelsOut(column=n, level=h) for n, h in zip(qi_names, best.levels)
                ],
                info_loss=round(best.loss, 6),
                info_loss_metric=INFO_LOSS_METRIC,
                n_combinations_explored=result.n_combinations_explored,
                summary=_summary(best.classes, ds),
                equivalence_classes=classes_out,
                classes_truncated=truncated,
                null_kept_in_sample=True,
                n_null_rows=n_null_rows,
                computation_trace=result.trace,
                warnings=ds.warnings,
                service_version=__version__,
            )
            if persist:
                self._persist_and_audit(schema_id, resp, "succeeded")
            return resp
        except ServiceError:
            raise
        except Exception:  # noqa: BLE001 - 未预期异常显式归类，不吞掉
            self.audit.append("analyze", "error", run_id=run_id, detail={"fingerprint": fp})
            log_event(self.log, "internal_error", level=40, run_id=run_id, fingerprint=fp)
            raise
        finally:
            from app.core.logging_setup import _input_fp_var, _run_id_var

            _run_id_var.reset(t1)
            _input_fp_var.reset(t2)

    # ------------------------------------------------------------------ #
    def _persist_and_audit(
        self, schema_id: str | None, resp: RunResponse, audit_status: str
    ) -> None:
        record = resp.model_dump(mode="json")
        if schema_id:
            try:
                self.store.save_run(record)
            except Exception:
                self.audit.append(
                    "persist_run", "error", run_id=resp.run_id, detail={"schema_id": schema_id}
                )
                raise
        self.audit.append(
            "analyze",
            audit_status,
            run_id=resp.run_id,
            detail={
                "schema_id": schema_id,
                "status": resp.status,
                "failure_code": resp.failure_code,
                "k": resp.k,
                "l": resp.l,
                "info_loss": resp.info_loss,
                "n_combinations_explored": resp.n_combinations_explored,
                "n_classes": resp.summary.n_classes if resp.summary else None,
            },
        )

    # ------------------------------------------------------------------ #
    def run_stored(self, schema_id: str, k: int, l: int) -> RunResponse:
        """从密文存储取出提交，用新阈值重放分析（状态隔离演示）。"""
        secret = self.store.load_schema_secret(schema_id)
        payload = DatasetIn(
            name=schema_id,
            columns=self._columns_from_secret(secret),
            rows=secret["rows"],
            k=k,
            l=l,
        )
        return self.analyze(payload, schema_id=schema_id)

    @staticmethod
    def _columns_from_secret(secret: dict[str, Any]) -> list[Any]:
        # 密文负载里只存了 name/role；层级需要从原提交恢复——
        # 为保证可重放，层级随密文一起存（在 submit 时补充）。
        return secret["column_specs"]
