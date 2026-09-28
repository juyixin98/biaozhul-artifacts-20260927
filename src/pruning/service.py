"""服务编排：HTTP 边界与核心模块之间的转换与串联。"""
from __future__ import annotations

import uuid

from .adapter import ReadOptions, discover_table
from .catalog import MetadataCatalog
from .kernel import PruningKernel
from .logging_config import event, get_logger
from .model import Predicate, PredicateKind
from .validation import FullScanValidator
from .versions import DATE_TRANSFORM_VERSION, version_bundle

_LOG = get_logger("pruning.service")

_KIND_MAP = {
    "range": PredicateKind.RANGE,
    "eq": PredicateKind.EQ,
    "in": PredicateKind.IN,
    "is_null": PredicateKind.IS_NULL,
    "not_null": PredicateKind.NOT_NULL,
}


def to_core_predicate(p) -> Predicate:
    try:
        kind = _KIND_MAP[p.kind]
    except KeyError as exc:
        raise ValueError(f"未知谓词类型 {p.kind!r}") from exc
    return Predicate(
        column=p.column, kind=kind, value=p.value,
        values=tuple(p.values) if p.values is not None else None,
        lower=p.lower, upper=p.upper,
        lower_inclusive=p.lower_inclusive, upper_inclusive=p.upper_inclusive)


class PruningService:
    def __init__(self, config):
        self.config = config
        self.catalog = MetadataCatalog(config.db_path)
        self.kernel = PruningKernel(DATE_TRANSFORM_VERSION)
        self.validator = FullScanValidator()

    def register(self, req) -> dict:
        rid = f"reg-{uuid.uuid4().hex[:12]}"
        event(_LOG, 20, rid, "register:start",
              f"扫描目录注册表 {req.table}", table=req.table,
              location=f"{self.config.data_root}/{req.table}")
        opts = ReadOptions(
            truncated_string_columns=tuple(req.truncated_string_columns),
            truncate_prefix_len=req.truncate_prefix_len,
            missing_stat_columns=tuple(req.missing_stat_columns))
        metadata = discover_table(self.config.data_root, req.table,
                                  req.partition_column, opts)
        summary = self.catalog.register_table(metadata, self.config.data_root)
        summary["request_id"] = rid
        summary["versions"] = version_bundle()
        event(_LOG, 20, rid, "register:done", f"注册完成 {summary}",
              table=req.table, extra=summary)
        return summary

    def _plan(self, table, predicates, rid):
        metadata = self.catalog.load_table(table)
        event(_LOG, 20, rid, "plan:kernel", "执行两级裁剪内核", table=table,
              extra={"predicates": len(predicates)})
        plan = self.kernel.plan(metadata, predicates, rid)
        self.catalog.save_plan_audit(plan, predicates)
        return metadata, plan

    def plan_only(self, req) -> dict:
        rid = req.request_id or f"plan-{uuid.uuid4().hex[:12]}"
        preds = [to_core_predicate(p) for p in req.predicates]
        _, plan = self._plan(req.table, preds, rid)
        out = plan.to_dict()
        out["versions"] = version_bundle()
        for note in plan.notes:
            event(_LOG, 30, rid, "plan:uncertain", note, table=req.table, uncertain=True)
        return out

    def validate(self, req) -> dict:
        rid = req.request_id or f"val-{uuid.uuid4().hex[:12]}"
        preds = [to_core_predicate(p) for p in req.predicates]
        metadata, plan = self._plan(req.table, preds, rid)
        report = self.validator.validate(metadata, preds, plan, rid)
        level = 40 if report.status == "fail" else 20
        for fail in report.failures:
            event(_LOG, level, rid, "validate:failure", fail["message"],
                  table=req.table, failure_category=fail["category"],
                  location=fail.get("file_id"), uncertain=(
                      fail["category"] == "selected_no_match"))
        event(_LOG, 20, rid, "validate:done",
              f"全扫描校验 status={report.status} "
              f"零漏行={report.zero_missed_matches} 层裁剪={report.layers}",
              table=req.table, extra=report.layers)
        out = report.to_dict()
        out["plan"] = plan.to_dict()
        out["versions"] = version_bundle()
        return out
