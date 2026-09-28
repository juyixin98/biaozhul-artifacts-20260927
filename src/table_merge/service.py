"""服务层：把存储（元数据事务）与内核（纯三方合并）编排成用例。

* 计划（merge plan）是不可变的：由 (base, dev, main) 三元组内容哈希确定 plan_id，
  冲突解决动作绑定这三方快照 ID，换了任何一侧都得到新计划；
* 合并提交永远保留两条父引用，且目标分支只允许在“仍指向读取时的 main 头”时推进，
  不允许用重新导入主分支快照的方式覆盖分支历史。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ConflictStateError, InvalidResolutionError
from .format_adapter import validate_rows
from .logging_setup import get_logger
from .merge_kernel import (
    apply_resolutions,
    ensure_compatible_schemas,
    three_way_merge,
    validate_resolution,
)
from .models import (
    ResolutionAction,
    Snapshot,
    TableSchema,
    key_string,
    stable_hash,
)
from .storage import MetadataStore

log = get_logger()


@dataclass
class MergePlan:
    plan_id: str
    table: str
    base_commit_id: str
    dev_commit_id: str
    main_commit_id: str
    base_snapshot_id: str
    dev_snapshot_id: str
    main_snapshot_id: str
    expected_head_commit_id: str
    target_branch: str
    report: Any  # MergeReport（含行级判定，不入库——解决结果入库）

    def identity(self) -> dict:
        return {
            "base_snapshot_id": self.base_snapshot_id,
            "dev_snapshot_id": self.dev_snapshot_id,
            "main_snapshot_id": self.main_snapshot_id,
            "target_branch": self.target_branch,
            "expected_head_commit_id": self.expected_head_commit_id,
        }


class MergeService:
    def __init__(self, store: MetadataStore):
        self.store = store

    # ---- 摄取 / 提交 / 分支 -----------------------------------------------

    def ingest_snapshot(self, payload: dict) -> dict:
        schema = TableSchema.from_dict(payload["schema"])
        rows = validate_rows(payload.get("rows", []), schema)
        snapshot = self.store.materialize_snapshot(rows, schema)
        log.info(
            "event=snapshot_ingested snapshot_id=%s table=%s rows=%d content_hash=%s reused=%s",
            snapshot.snapshot_id, snapshot.table, snapshot.row_count,
            snapshot.content_hash[:12], snapshot.reused,
        )
        return {
            "snapshot_id": snapshot.snapshot_id,
            "table": snapshot.table,
            "row_count": snapshot.row_count,
            "content_hash": snapshot.content_hash,
            "schema": schema.to_dict(),
        }

    def initialize_main(self, snapshot_id: str, message: str, author: str) -> dict:
        if self.store.list_branches():
            raise ConflictStateError(
                "repository already initialized; create commits on branches instead",
                details={"branches": [b["name"] for b in self.store.list_branches()]},
            )
        commit = self.store.create_commit(snapshot_id, None, message, author)
        self.store.create_branch("main", commit["commit_id"])
        log.info("event=main_initialized commit_id=%s snapshot_id=%s",
                 commit["commit_id"], snapshot_id)
        return self.store.get_branch("main") | {"commit": self.store.get_commit(commit["commit_id"])}

    def create_branch(self, name: str, ref: dict) -> dict:
        commit_id, _snapshot_id = self.store.resolve_ref(ref)
        branch = self.store.create_branch(name, commit_id)
        log.info("event=branch_created branch=%s head=%s", name, commit_id)
        return branch

    def commit(self, branch_name: str, snapshot_id: str, message: str, author: str) -> dict:
        branch = self.store.get_branch(branch_name)
        # 可选：schema 必须与分支当前快照兼容（同一张表）
        _new_snap = self.store.get_snapshot(snapshot_id)
        head_snap = self.store.get_snapshot(
            self.store.get_commit(branch["commit_id"])["snapshot_id"]
        )
        self._require_same_table(head_snap, _new_snap)
        commit = self.store.create_commit(
            snapshot_id, branch["commit_id"], message, author, branch_name=branch_name
        )
        log.info("event=commit_created branch=%s commit_id=%s parent=%s snapshot_id=%s",
                 branch_name, commit["commit_id"], branch["commit_id"], snapshot_id)
        return commit

    @staticmethod
    def _require_same_table(a: Snapshot, b: Snapshot) -> None:
        if a.table != b.table:
            from .errors import SchemaMismatchError
            raise SchemaMismatchError(
                f"snapshots belong to different tables: {a.table!r} vs {b.table!r}",
                details={"table_a": a.table, "table_b": b.table},
            )

    # ---- 合并计划 ----------------------------------------------------------

    def plan_merge(self, dev_ref: dict, main_ref: dict, target_branch: str = "main") -> MergePlan:
        dev_commit_id, _ = self.store.resolve_ref(dev_ref)
        main_commit_id, _ = self.store.resolve_ref(main_ref)
        return self._build_plan(dev_commit_id, main_commit_id, target_branch)

    def rebuild_plan(self, dev_ref: dict, target_branch: str,
                     expected_plan_id: str) -> MergePlan:
        """按 plan_id 校验并重建计划。

        计划不持有服务端会话状态：它的全部输入都已落库（三方快照 + 目标分支头），
        任何时候重建都得到相同的行级判定；已保存的解决结果按 plan_id 复用。
        main 侧恒为目标分支当前头——若 main 在计划生成后又前进，重建会得到
        不同 plan_id，旧解决结果不会被错用。
        """
        main_commit_id = self.store.get_branch(target_branch)["commit_id"]
        dev_commit_id, _ = self.store.resolve_ref(dev_ref)
        plan = self._build_plan(dev_commit_id, main_commit_id, target_branch)
        if plan.plan_id != expected_plan_id:
            raise ConflictStateError(
                f"plan {expected_plan_id!r} is stale: the three-way inputs changed; "
                "request a fresh plan before resolving or committing",
                details={"submitted_plan_id": expected_plan_id,
                         "current_plan_id": plan.plan_id},
            )
        return plan

    def _build_plan(self, dev_commit_id: str, main_commit_id: str,
                    target_branch: str) -> MergePlan:
        base = self.store.find_merge_base(dev_commit_id, main_commit_id)

        branch = self.store.get_branch(target_branch)
        if branch["commit_id"] != main_commit_id:
            raise ConflictStateError(
                f"target branch {target_branch!r} head ({branch['commit_id']}) does not match "
                f"the requested main-side commit ({main_commit_id}); re-plan against current head",
                details={"branch_head": branch["commit_id"],
                         "requested_commit": main_commit_id},
            )

        base_snap, base_rows = self.store.read_snapshot_rows(base["base_snapshot_id"])
        dev_snap = self.store.get_snapshot(
            self.store.get_commit(dev_commit_id)["snapshot_id"])
        main_snap = self.store.get_snapshot(
            self.store.get_commit(main_commit_id)["snapshot_id"])
        _b, dev_rows = self.store.read_snapshot_rows(dev_snap.snapshot_id)
        _m, main_rows = self.store.read_snapshot_rows(main_snap.snapshot_id)
        log.info(
            "event=merge_planning base=%s dev=%s main=%s rows(b/d/m)=%d/%d/%d",
            base["base_commit_id"], dev_commit_id, main_commit_id,
            len(base_rows), len(dev_rows), len(main_rows),
        )
        schema = ensure_compatible_schemas(base_snap, dev_snap, main_snap)
        report = three_way_merge(
            schema, base_rows, dev_rows, main_rows,
            base_snapshot_id=base_snap.snapshot_id,
            dev_snapshot_id=dev_snap.snapshot_id,
            main_snapshot_id=main_snap.snapshot_id,
        )
        for step in report.steps:
            log.info("event=kernel_step %s", step)
        for decision in report.conflicts:
            log.warning(
                "event=conflict key=%s decision=%s basis=%s",
                key_string(decision.key), decision.decision.value, decision.basis,
            )

        plan = MergePlan(
            plan_id="plan_" + stable_hash({
                "base_snapshot_id": base_snap.snapshot_id,
                "dev_snapshot_id": dev_snap.snapshot_id,
                "main_snapshot_id": main_snap.snapshot_id,
                "target_branch": target_branch,
                "expected_head_commit_id": main_commit_id,
            })[:16],
            table=schema.table,
            base_commit_id=base["base_commit_id"],
            dev_commit_id=dev_commit_id,
            main_commit_id=main_commit_id,
            base_snapshot_id=base_snap.snapshot_id,
            dev_snapshot_id=dev_snap.snapshot_id,
            main_snapshot_id=main_snap.snapshot_id,
            expected_head_commit_id=main_commit_id,
            target_branch=target_branch,
            report=report,
        )
        log.info(
            "event=merge_plan_ready plan_id=%s counts=%s conflicts=%d",
            plan.plan_id, report.counts(), len(report.conflicts),
        )
        return plan

    def plan_to_dict(self, plan: MergePlan) -> dict:
        data = plan.report.to_plan_dict()
        data.update({
            "plan_id": plan.plan_id,
            "table": plan.table,
            "base_commit_id": plan.base_commit_id,
            "dev_commit_id": plan.dev_commit_id,
            "main_commit_id": plan.main_commit_id,
            "target_branch": plan.target_branch,
            "expected_head_commit_id": plan.expected_head_commit_id,
            "has_conflicts": bool(plan.report.conflicts),
        })
        return data

    # ---- 冲突解决 ----------------------------------------------------------

    def save_resolutions(self, plan: MergePlan, items: list[dict]) -> dict:
        valid_keys = {key_string(d.key): d for d in plan.report.conflicts}
        normalized: list[dict] = []
        for item in items:
            row_key = item.get("row_key")
            if row_key not in valid_keys:
                raise InvalidResolutionError(
                    f"row_key {row_key!r} is not a conflict of plan {plan.plan_id}",
                    details={"row_key": row_key,
                             "conflict_keys": sorted(valid_keys)},
                )
            decision = valid_keys[row_key]
            action = self._parse_action(item.get("action"))
            field_picks = item.get("field_picks")
            # 内核做权威合法性校验（允许的动作集合、FIELD_PICK 取值）
            validate_resolution(decision.decision, action, field_picks)
            normalized.append({
                "row_key": row_key,
                "decision": decision.decision.value,
                "action": action.value,
                "field_picks": field_picks,
                "base_snapshot_id": plan.base_snapshot_id,
                "dev_snapshot_id": plan.dev_snapshot_id,
                "main_snapshot_id": plan.main_snapshot_id,
            })
        count = self.store.save_resolutions(plan.plan_id, normalized)
        resolved = sorted(set(self.store.load_resolutions(plan.plan_id)))
        log.info("event=resolutions_saved plan_id=%s count=%d resolved_total=%d bound_to=%s/%s/%s",
                 plan.plan_id, count, len(resolved), plan.base_snapshot_id,
                 plan.dev_snapshot_id, plan.main_snapshot_id)
        return {"plan_id": plan.plan_id, "saved": count,
                "resolved_keys": resolved,
                "total_conflicts": len(valid_keys)}

    @staticmethod
    def _parse_action(raw: Any) -> ResolutionAction:
        try:
            return ResolutionAction(raw)
        except (ValueError, TypeError):
            raise InvalidResolutionError(
                f"unknown resolution action {raw!r}",
                details={"action": raw,
                         "valid": [a.value for a in ResolutionAction]},
            )

    # ---- 合并提交 ----------------------------------------------------------

    def commit_merge(self, plan: MergePlan, message: str, author: str,
                     resolutions_override: list[dict] | None = None) -> dict:
        # 计划不可变校验：三方快照必须与计划一致（plan 对象本身在服务端重建时保证）
        if resolutions_override is not None:
            self.save_resolutions(plan, resolutions_override)
        stored = self.store.load_resolutions(plan.plan_id)

        conflict_keys = [key_string(d.key) for d in plan.report.conflicts]
        missing = [k for k in conflict_keys if k not in stored]
        if missing:
            raise ConflictStateError(
                f"cannot commit merge: {len(missing)} conflict(s) unresolved",
                details={"plan_id": plan.plan_id, "unresolved": missing},
            )

        schema = self.store.get_snapshot(plan.base_snapshot_id).schema
        keyed: dict[tuple, dict] = {}
        for row_key, payload in stored.items():
            # 反查决策对象
            decision = next(d for d in plan.report.conflicts if key_string(d.key) == row_key)
            if payload["base_snapshot_id"] != plan.base_snapshot_id or \
                    payload["dev_snapshot_id"] != plan.dev_snapshot_id or \
                    payload["main_snapshot_id"] != plan.main_snapshot_id:
                raise InvalidResolutionError(
                    f"resolution for {row_key} is bound to a different three-way snapshot set "
                    "than the current plan; resolve against this plan",
                    details={"row_key": row_key,
                             "plan": plan.identity(),
                             "resolution_bound_to": {
                                 "base": payload["base_snapshot_id"],
                                 "dev": payload["dev_snapshot_id"],
                                 "main": payload["main_snapshot_id"],
                             }},
                )
            keyed[decision.key] = {
                "action": payload["action"],
                "field_picks": payload["field_picks"],
            }

        final_rows = apply_resolutions(schema, plan.report, keyed)
        snapshot = self.store.materialize_snapshot(final_rows, schema)

        action_counts: dict[str, int] = {}
        for payload in stored.values():
            action_counts[payload["action"]] = action_counts.get(payload["action"], 0) + 1
        summary = {
            "plan_id": plan.plan_id,
            "counts": plan.report.counts(),
            "resolution_actions": action_counts,
            "final_row_count": len(final_rows),
        }
        commit = self.store.create_merge_commit(
            snapshot_id=snapshot.snapshot_id,
            base_commit_id=plan.base_commit_id,
            parent1_dev_commit_id=plan.dev_commit_id,
            parent2_main_commit_id=plan.main_commit_id,
            base_snapshot_id=plan.base_snapshot_id,
            parent1_snapshot_id=plan.dev_snapshot_id,
            parent2_snapshot_id=plan.main_snapshot_id,
            resolution_summary=summary,
            target_branch=plan.target_branch,
            expected_head_commit_id=plan.expected_head_commit_id,
            message=message,
            author=author,
        )
        log.info(
            "event=merge_committed merge_commit_id=%s parents=[%s,%s] base=%s "
            "snapshot_id=%s rows=%d",
            commit["commit_id"], plan.dev_commit_id, plan.main_commit_id,
            plan.base_commit_id, snapshot.snapshot_id, len(final_rows),
        )
        return {
            "merge_commit": commit,
            "snapshot_id": snapshot.snapshot_id,
            "row_count": len(final_rows),
            "resolution_summary": summary,
            "parent_commit_ids": commit["parent_commit_ids"],
        }

    # ---- 读取 --------------------------------------------------------------

    def lineage(self, ref: dict) -> dict:
        commit_id, _ = self.store.resolve_ref(ref)
        commit = self.store.get_commit(commit_id)
        return {
            "commit_id": commit["commit_id"],
            "snapshot_id": commit["snapshot_id"],
            "parent_commit_ids": commit["parent_commit_ids"],
            "is_merge": "merge" in commit,
            "merge_detail": commit.get("merge"),
        }

    def snapshot_rows(self, snapshot_id: str) -> dict:
        snap, rows = self.store.read_snapshot_rows(snapshot_id)
        return {
            "snapshot_id": snap.snapshot_id,
            "table": snap.table,
            "schema": snap.schema.to_dict(),
            "row_count": len(rows),
            "rows": rows,
        }
