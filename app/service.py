"""服务编排：API 与核心算法之间的用例层。

集中处理事务边界、诊断中间状态与状态机转移（planned -> applied 仅一次）。
核心算法（planning/apply）不依赖本层，可被测试独立调用并与参考实现对账。
"""
from __future__ import annotations

from collections.abc import Iterator

from . import planning
from .apply import apply_plan_stream
from .config import LIMITS
from .diagnostics import Diag
from .errors import (
    PayloadTooLargeError,
    PlanAlreadyAppliedError,
    PlanNotFoundError,
    SourceNotFoundError,
    SourceVersionMismatchError,
)
from .planning import Plan
from .schemas import (
    ApplyResult,
    DisplacedHitOut,
    GroupOut,
    PlanDetail,
    PlanRequest,
    PlanSummary,
    ReplacementOut,
    RuleIn,
    RuleValidationOut,
    SourceSpecOut,
)
from .storage import Repository
from .textutil import ByteOffsetMap, make_spec, normalize_text


class Service:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ------------------------------------------------------------------ #
    # 源
    # ------------------------------------------------------------------ #
    def upload_source(self, text: str) -> dict:
        text = normalize_text(text)
        if len(text) > LIMITS.max_text_chars:
            raise PayloadTooLargeError(
                "source text exceeds char budget",
                details={"char_len": len(text), "limit": LIMITS.max_text_chars},
            )
        return self.repo.create_source(text)

    def get_source(self, source_id: str) -> dict:
        found = self.repo.get_source(source_id)
        if found is None:
            raise SourceNotFoundError("unknown source_id", details={"source_id": source_id})
        return found

    def replace_source(self, source_id: str, text: str) -> dict:
        text = normalize_text(text)
        if len(text) > LIMITS.max_text_chars:
            raise PayloadTooLargeError(
                "source text exceeds char budget",
                details={"char_len": len(text), "limit": LIMITS.max_text_chars},
            )
        self.get_source(source_id)  # 不存在则抛 404
        return self.repo.update_source_text(source_id, text)

    # ------------------------------------------------------------------ #
    # 规则前置校验
    # ------------------------------------------------------------------ #
    def validate_rules(self, rules: list[RuleIn]) -> list[RuleValidationOut]:
        """逐条编译/解析但不落计划；每条独立报告成败与失败原因。"""
        out: list[RuleValidationOut] = []
        for ri in rules:
            try:
                prepared = planning.prepare_rules([ri])[0]
                out.append(
                    RuleValidationOut(
                        rule_id=ri.rule_id,
                        ok=True,
                        group_count=prepared.compiled.group_count,
                        group_names=[n for n in prepared.compiled.group_names[1:] if n],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 此处要把失败也结构化返回
                out.append(
                    RuleValidationOut(
                        rule_id=ri.rule_id,
                        ok=False,
                        group_count=0,
                        group_names=[],
                        reason=f"{getattr(exc, 'code', type(exc).__name__)}: {exc}",
                    )
                )
        return out

    # ------------------------------------------------------------------ #
    # 计划
    # ------------------------------------------------------------------ #
    def create_plan(self, req: PlanRequest, diag: Diag | None = None) -> tuple[PlanSummary | PlanDetail, str]:
        diag = diag or Diag(self.repo)
        loaded = self.repo.get_text(req.source_id)
        if loaded is None:
            diag.warn("plan", "source_missing", "source not found", source_id=req.source_id)
            raise SourceNotFoundError("unknown source_id", details={"source_id": req.source_id})
        text, source_version, bound_spec = loaded
        diag.info(
            "plan", "source_loaded", "source loaded for planning",
            source_id=req.source_id, source_version=source_version,
            char_len=bound_spec.char_len, byte_len=bound_spec.byte_len,
            sha256=bound_spec.sha256, rule_count=len(req.rules),
        )

        plan = planning.build_plan(text, req.rules)
        # 关键中间状态：入选骨架与被淘汰命中（截断防超大计划刷爆日志）
        diag.info(
            "plan", "plan_built", "plan constructed",
            chosen=len(plan.chosen), displaced=len(plan.displaced),
            zero_width=plan.zero_width_count,
            chosen_spans=[[c.hit.start, c.hit.end, c.rule.rule_id] for c in plan.chosen[:200]],
            displaced_sample=[d.__dict__ for d in plan.displaced[:200]],
        )

        if req.dry_run:
            detail = self._plan_detail(req.source_id, source_version, text, plan, req.rules)
            detail.plan_id = "dry-run"
            return detail, diag.run_id

        plan_id = self.repo.save_plan(plan, req.source_id, source_version, req.rules)
        diag.info("plan", "plan_saved", "plan persisted", plan_id=plan_id)
        summary = PlanSummary(
            plan_id=plan_id,
            source_id=req.source_id,
            source_version=source_version,
            source_spec=SourceSpecOut(**plan.source_spec.__dict__),
            rule_count=len(plan.rules),
            replacement_count=len(plan.chosen),
            zero_width_count=plan.zero_width_count,
            status="planned",
        )
        return summary, diag.run_id

    def _plan_detail(
        self,
        source_id: str,
        source_version: int,
        text: str,
        plan: Plan,
        rules_in: list[RuleIn],
    ) -> PlanDetail:
        bm = ByteOffsetMap(text)
        replacements: list[ReplacementOut] = []
        for idx, cand in enumerate(plan.chosen):
            h = cand.hit
            groups = []
            for g in h.groups:
                if g.text is None:
                    b0 = b1 = -1
                else:
                    b0, b1 = bm.char_span_to_byte_span(g.char_start, g.char_end)
                groups.append(
                    GroupOut(
                        index=g.index, name=g.name, text=g.text,
                        char_start=g.char_start, char_end=g.char_end,
                        byte_start=b0, byte_end=b1,
                    )
                )
            replacements.append(
                ReplacementOut(
                    index=idx,
                    rule_id=cand.rule.rule_id,
                    priority=cand.rule.priority,
                    declaration_order=cand.rule.declaration_order,
                    char_start=h.start,
                    char_end=h.end,
                    byte_start=cand.byte_start,
                    byte_end=cand.byte_end,
                    matched=h.text,
                    replacement=cand.replacement,
                    zero_width=h.is_zero_width,
                    groups=groups,
                )
            )
        return PlanDetail(
            plan_id="",
            source_id=source_id,
            source_version=source_version,
            source_spec=SourceSpecOut(**plan.source_spec.__dict__),
            rule_count=len(plan.rules),
            replacement_count=len(plan.chosen),
            zero_width_count=plan.zero_width_count,
            status="planned",
            rules=rules_in,
            replacements=replacements,
            displaced=[
                DisplacedHitOut(
                    rule_id=d.rule_id, char_start=d.char_start, char_end=d.char_end,
                    reason=d.reason,  # type: ignore[arg-type]
                )
                for d in plan.displaced
            ],
        )

    def get_plan_detail(self, plan_id: str) -> tuple[PlanDetail, Plan, str, int]:
        header = self.repo.get_plan_header(plan_id)
        if header is None:
            raise PlanNotFoundError("unknown plan_id", details={"plan_id": plan_id})
        source_id = header["source_id"]
        bound_version = header["source_version"]
        loaded = self.repo.get_text(source_id, bound_version)
        if loaded is None:
            raise SourceNotFoundError(
                "bound source version missing",
                details={"source_id": source_id, "version": bound_version},
            )
        text, _, _ = loaded
        rules_in = [RuleIn(**r) for r in self.repo.get_plan_rules(plan_id)]
        plan = planning.build_plan(text, rules_in)
        detail = self._plan_detail(source_id, bound_version, text, plan, rules_in)
        detail.plan_id = plan_id
        detail.status = header["status"]  # type: ignore[assignment]
        detail.applied_version = header["applied_version"]
        return detail, plan, text, bound_version

    # ------------------------------------------------------------------ #
    # 应用
    # ------------------------------------------------------------------ #
    def _load_applyable(self, plan_id: str, diag: Diag):
        header = self.repo.get_plan_header(plan_id)
        if header is None:
            raise PlanNotFoundError("unknown plan_id", details={"plan_id": plan_id})
        if header["status"] == "applied":
            raise PlanAlreadyAppliedError(
                "plan already applied",
                details={"plan_id": plan_id, "applied_version": header["applied_version"]},
            )
        source_id = header["source_id"]
        bound_version = header["source_version"]
        loaded = self.repo.get_text(source_id)
        if loaded is None:
            raise SourceNotFoundError("source vanished", details={"source_id": source_id})
        text, current_version, _ = loaded

        if current_version != bound_version:
            diag.error(
                "apply", "version_mismatch",
                "source version differs from plan binding",
                current_version=current_version, bound_version=bound_version,
            )
            raise SourceVersionMismatchError(
                "source version does not match the version the plan was built against",
                details={
                    "current_version": current_version,
                    "plan_bound_version": bound_version,
                },
            )
        current_spec = make_spec(text)
        if current_spec.as_tuple() != (
            header["sha256"], header["byte_len"], header["char_len"]
        ):
            diag.error(
                "apply", "digest_mismatch",
                "source digest differs despite same version",
                current_sha256=current_spec.sha256, bound_sha256=header["sha256"],
            )
            raise SourceVersionMismatchError(
                "source digest does not match the plan binding",
                details={"current_sha256": current_spec.sha256, "bound_sha256": header["sha256"]},
            )

        rules_in = [RuleIn(**r) for r in self.repo.get_plan_rules(plan_id)]
        plan = planning.build_plan(text, rules_in)
        return header, text, bound_version, plan, rules_in

    def apply_plan_stream(
        self, plan_id: str, diag: Diag, chunk_chars: int | None = None
    ) -> tuple[Iterator[str], dict, str]:
        header, text, bound_version, plan, _rules = self._load_applyable(plan_id, diag)
        source_id = header["source_id"]
        diag.info(
            "apply", "guard_ok", "source guard passed",
            source_version=bound_version, replaced=len(plan.chosen),
        )

        def generator() -> Iterator[str]:
            chunks: list[str] = []
            for ch in apply_plan_stream(
                text, plan,
                source_version=bound_version,
                expected_version=bound_version,
                chunk_chars=chunk_chars or LIMITS.apply_chunk_chars,
            ):
                chunks.append(ch)
                yield ch
            output_text = "".join(chunks)
            output_spec = make_spec(output_text)
            diag.info(
                "apply", "output_built", "output assembled",
                output_char_len=output_spec.char_len, output_sha256=output_spec.sha256,
            )
            out_version = self.repo.add_applied_version(source_id, output_text)
            self.repo.mark_plan_applied(plan_id, out_version)
            diag.info("apply", "committed", "plan applied", output_version=out_version)

        meta = {
            "plan_id": plan_id,
            "source_id": source_id,
            "source_version": bound_version,
            "replaced": len(plan.chosen),
        }
        return generator, meta, diag.run_id

    def apply_plan_collect(self, plan_id: str, diag: Diag | None = None) -> tuple[ApplyResult, str]:
        diag = diag or Diag(self.repo)
        generator, meta, run_id = self.apply_plan_stream(plan_id, diag)
        output_text = "".join(generator())
        header = self.repo.get_plan_header(plan_id)
        loaded = self.repo.get_text(meta["source_id"])
        _t, _v, spec = loaded
        result = ApplyResult(
            plan_id=plan_id,
            source_id=meta["source_id"],
            source_version=meta["source_version"],
            output_version=header["applied_version"],
            output_spec=SourceSpecOut(**spec.__dict__),
            char_len=spec.char_len,
            replaced=meta["replaced"],
        )
        return result, run_id
