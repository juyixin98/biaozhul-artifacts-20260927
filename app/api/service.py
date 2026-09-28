"""Use-case orchestration.

One function per user goal.  This layer owns *workflow* (normalize -> index ->
compile -> plan -> persist -> apply) and the hard size caps; pure modules own
the mechanics.  Keeping it thin makes the same operations callable directly in
tests without spinning up HTTP.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from ..errors import EmptyTextError, TextTooLargeError
from ..planner import (
    PlannerLimits,
    apply_plan_stream,
    build_plan,
)
from ..planner.model import Plan
from ..planner.rules import RuleSpec, compile_rules
from ..storage.repository import (
    Repository,
    StoredApplication,
    StoredPlan,
    StoredRuleset,
    StoredSource,
)
from ..textspec import ByteIndex, normalize_source, sha256_hex


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    max_source_bytes: int = 64 * 1024 * 1024
    max_candidates_per_rule: int = 200_000
    max_edits: int = 200_000
    max_output_bytes: int = 256 * 1024 * 1024
    apply_chunk_size: int = 64 * 1024

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls(
            max_source_bytes=_env_int("NRS_MAX_SOURCE_BYTES", 64 * 1024 * 1024),
            max_candidates_per_rule=_env_int("NRS_MAX_CANDIDATES", 200_000),
            max_edits=_env_int("NRS_MAX_EDITS", 200_000),
            max_output_bytes=_env_int("NRS_MAX_OUTPUT_BYTES", 256 * 1024 * 1024),
            apply_chunk_size=_env_int("NRS_CHUNK_SIZE", 64 * 1024),
        )

    def planner_limits(self) -> PlannerLimits:
        return PlannerLimits(
            max_candidates_per_rule=self.max_candidates_per_rule,
            max_edits=self.max_edits,
            max_output_bytes=self.max_output_bytes,
        )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Service:
    def __init__(self, repo: Repository, config: ServiceConfig | None = None) -> None:
        self.repo = repo
        self.config = config or ServiceConfig.from_env()

    # -------------------------------------------------------------- sources
    def upload_source(
        self,
        source_id: str,
        text: str | bytes,
        *,
        normalize_newlines: bool = False,
    ) -> StoredSource:
        if isinstance(text, str):
            raw_len = len(text.encode("utf-8"))
        else:
            raw_len = len(text)
        if raw_len > self.config.max_source_bytes:
            raise TextTooLargeError(
                "source exceeds hard size cap",
                observed=raw_len,
                limit=self.config.max_source_bytes,
            )
        normalized = normalize_source(text, normalize_newlines=normalize_newlines)
        if normalized.size == 0:
            raise EmptyTextError("source must contain at least one byte")
        index = ByteIndex(normalized.data)
        digest = sha256_hex(normalized.data)
        return self.repo.put_source(
            source_id,
            normalized.data,
            codepoints=index.codepoints,
            sha256=digest,
            normalize_newlines=normalize_newlines,
        )

    def get_source(self, source_id: str) -> StoredSource:
        return self.repo.get_source(source_id)

    # -------------------------------------------------------------- rulesets
    def put_ruleset(self, ruleset_id: str, payload: list[dict]) -> StoredRuleset:
        specs = [
            RuleSpec(
                rule_id=r["rule_id"],
                pattern=r["pattern"],
                template=r["template"],
                priority=int(r.get("priority", 0)),
                flags=r.get("flags", ""),
                longest_match=bool(r.get("longest_match", False)),
                max_mem=int(r.get("max_mem", 8 * 1024 * 1024)),
                missing_capture=r.get("missing_capture", "error"),
            )
            for r in payload
        ]
        # Fail closed at rule creation: patterns compile and every template
        # reference must resolve statically before anything is persisted.
        compile_rules(specs)
        return self.repo.put_ruleset(ruleset_id, specs)

    # ---------------------------------------------------------------- plans
    def create_plan(self, source_id: str, ruleset_id: str) -> tuple[StoredPlan, object]:
        source = self.repo.get_source(source_id)
        ruleset = self.repo.get_ruleset(ruleset_id)
        result = build_plan(
            source.data,
            list(ruleset.rules),
            normalize_newlines=source.normalize_newlines,
            limits=self.config.planner_limits(),
        )
        plan_id = new_id("plan")
        stored = self.repo.put_plan(plan_id, result, ruleset_id=ruleset_id)
        return stored, result

    def get_plan_detail(self, plan_id: str) -> tuple[StoredPlan, Plan, list[dict]]:
        stored = self.repo.get_plan(plan_id)
        plan = Plan.from_json(stored.plan_json)
        decisions = self.repo.get_decisions(plan_id)
        return stored, plan, decisions

    # --------------------------------------------------------------- apply
    def apply_plan(
        self,
        plan_id: str,
        *,
        expected_sha256: str | None = None,
        source_id: str | None = None,
        save_result_as: str | None = None,
    ) -> tuple[StoredApplication, bytes]:
        stored = self.repo.get_plan(plan_id)
        if source_id is None:
            data = self.repo.get_source_by_sha(stored.source_sha256)
            source_id_for_record = _digest_alias(stored.source_sha256)
        else:
            source = self.repo.get_source(source_id)
            data = source.data
            source_id_for_record = source_id

        plan = Plan.from_json(stored.plan_json)
        result = apply_plan_stream(
            plan,
            data,
            expected_sha256=expected_sha256,
            chunk_size=self.config.apply_chunk_size,
        )
        out_digest = sha256_hex(result.output)

        new_source_id: str | None = None
        if save_result_as:
            index = ByteIndex(result.output)
            self.repo.put_source(
                save_result_as,
                result.output,
                codepoints=index.codepoints,
                sha256=out_digest,
                normalize_newlines=plan.normalize_newlines,
            )
            new_source_id = save_result_as

        record = self.repo.record_application(
            application_id=new_id("app"),
            plan_id=plan_id,
            source_id=source_id_for_record,
            expected_sha256=expected_sha256 or stored.source_sha256,
            result_sha256=out_digest,
            result_length=result.bytes_emitted,
            chunks_emitted=result.chunks_emitted,
            new_source_id=new_source_id,
        )
        return record, result.output


def _digest_alias(sha: str) -> str:
    return f"sha256:{sha[:16]}"
