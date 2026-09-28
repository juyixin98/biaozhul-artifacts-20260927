"""Repository: typed persistence operations over :class:`Database`.

The repository is the only layer that speaks SQL.  Everything it accepts or
returns is either a plain immutable dataclass, ``bytes`` or JSON text -- no
sqlite rows leak upward, so the API/planner layers do not depend on the
storage representation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..errors import (
    AlreadyAppliedError,
    PlanNotFound,
    RulesetNotFound,
    SourceNotFound,
)
from ..planner.model import PlanResult
from ..planner.rules import RuleSpec


@dataclass(frozen=True, slots=True)
class StoredSource:
    id: str
    version: int
    sha256: str
    length: int
    normalize_newlines: bool
    data: bytes


@dataclass(frozen=True, slots=True)
class StoredRuleset:
    id: str
    version: int
    rules: tuple[RuleSpec, ...]
    spec_json: str


@dataclass(frozen=True, slots=True)
class StoredPlan:
    id: str
    source_sha256: str
    source_length: int
    ruleset_id: str
    plan_json: str
    edit_count: int
    candidates_total: int
    candidates_dropped: int


@dataclass(frozen=True, slots=True)
class StoredApplication:
    id: str
    plan_id: str
    source_id: str
    expected_sha256: str
    result_sha256: str
    result_length: int
    new_source_id: str | None
    chunks_emitted: int


def _rule_to_dict(r: RuleSpec) -> dict:
    return {
        "rule_id": r.rule_id,
        "pattern": r.pattern,
        "template": r.template,
        "priority": r.priority,
        "flags": r.flags,
        "longest_match": r.longest_match,
        "max_mem": r.max_mem,
        "missing_capture": r.missing_capture,
    }


def _rule_from_dict(d: dict) -> RuleSpec:
    return RuleSpec(
        rule_id=d["rule_id"],
        pattern=d["pattern"],
        template=d["template"],
        priority=int(d.get("priority", 0)),
        flags=d.get("flags", ""),
        longest_match=bool(d.get("longest_match", False)),
        max_mem=int(d.get("max_mem", 8 * 1024 * 1024)),
        missing_capture=d.get("missing_capture", "error"),
    )


class Repository:
    def __init__(self, db) -> None:  # Database
        self.db = db

    # ---------------------------------------------------------------- sources
    def put_source(
        self,
        source_id: str,
        data: bytes,
        *,
        codepoints: int,
        sha256: str,
        normalize_newlines: bool = False,
    ) -> StoredSource:
        """Insert/replace a logical source. Returns the stored version row.

        Replacing the same id bumps its version and points the pointer at the
        new content; the old immutable version row remains for replay/audit.
        Re-uploading identical bytes is idempotent (same version returned).
        """
        with self.db.transaction() as cur:
            row = cur.execute(
                "SELECT version, current_sha256 FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                version = 1
            elif row["current_sha256"] == sha256:
                version = int(row["version"])
                return self.get_source(source_id)
            else:
                version = int(row["version"]) + 1

            cur.execute(
                "INSERT OR IGNORE INTO source_versions"
                "(sha256,data,length,codepoints,normalize_newlines) VALUES(?,?,?,?,?)",
                (sha256, sqlite3.Binary(data), len(data), codepoints, int(normalize_newlines)),
            )
            if row is None:
                cur.execute(
                    "INSERT INTO sources(id,version,current_sha256,current_length,"
                    "normalize_newlines) VALUES(?,?,?,?,?)",
                    (source_id, version, sha256, len(data), int(normalize_newlines)),
                )
            else:
                cur.execute(
                    "UPDATE sources SET version=?, current_sha256=?, "
                    "current_length=?, normalize_newlines=? WHERE id=?",
                    (version, sha256, len(data), int(normalize_newlines), source_id),
                )
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> StoredSource:
        row = self.db.conn.execute(
            "SELECT s.id, s.version, s.current_sha256 AS sha, s.current_length AS ln, "
            "s.normalize_newlines AS nn, v.data AS data "
            "FROM sources s JOIN source_versions v ON v.sha256=s.current_sha256 "
            "WHERE s.id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise SourceNotFound("no such source", source_id=source_id)
        return StoredSource(
            id=row["id"],
            version=int(row["version"]),
            sha256=row["sha"],
            length=int(row["ln"]),
            normalize_newlines=bool(row["nn"]),
            data=bytes(row["data"]),
        )

    def get_source_by_sha(self, sha256: str) -> bytes:
        row = self.db.conn.execute(
            "SELECT data FROM source_versions WHERE sha256=?", (sha256,)
        ).fetchone()
        if row is None:
            raise SourceNotFound("no source version with digest", sha256=sha256)
        return bytes(row["data"])

    # ---------------------------------------------------------------- rulesets
    def put_ruleset(self, ruleset_id: str, rules: list[RuleSpec]) -> StoredRuleset:
        payload = [_rule_to_dict(r) for r in rules]
        spec_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.db.transaction() as cur:
            row = cur.execute("SELECT version FROM rulesets WHERE id=?", (ruleset_id,)).fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO rulesets(id,spec_json,rule_count,version) VALUES(?,?,?,1)",
                    (ruleset_id, spec_json, len(rules)),
                )
                version = 1
            else:
                version = int(row["version"]) + 1
                cur.execute(
                    "UPDATE rulesets SET spec_json=?, rule_count=?, version=? WHERE id=?",
                    (spec_json, len(rules), version, ruleset_id),
                )
        return StoredRuleset(
            id=ruleset_id,
            version=version,
            rules=tuple(rules),
            spec_json=spec_json,
        )

    def get_ruleset(self, ruleset_id: str) -> StoredRuleset:
        row = self.db.conn.execute(
            "SELECT id, version, spec_json FROM rulesets WHERE id=?", (ruleset_id,)
        ).fetchone()
        if row is None:
            raise RulesetNotFound("no such ruleset", ruleset_id=ruleset_id)
        rules = tuple(_rule_from_dict(d) for d in json.loads(row["spec_json"]))
        return StoredRuleset(id=row["id"], version=int(row["version"]), rules=rules,
                            spec_json=row["spec_json"])

    # ------------------------------------------------------------------- plans
    def put_plan(
        self,
        plan_id: str,
        result: PlanResult,
        *,
        ruleset_id: str,
    ) -> StoredPlan:
        plan = result.plan
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO plans(id,source_sha256,source_length,ruleset_id,plan_json,"
                "edit_count,candidates_total,candidates_dropped) VALUES(?,?,?,?,?,?,?,?)",
                (
                    plan_id,
                    plan.source_sha256,
                    plan.source_length,
                    ruleset_id,
                    plan.to_json(),
                    plan.edit_count,
                    result.candidates_total,
                    result.candidates_dropped,
                ),
            )
            for ordinal, decision in enumerate(result.decisions):
                cur.execute(
                    "INSERT INTO plan_decisions(plan_id,ordinal,stage,rule_id,start,end,"
                    "zero_width,reason,conflicts_with) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        ordinal,
                        decision.stage,
                        decision.rule_id,
                        decision.start,
                        decision.end,
                        int(decision.zero_width),
                        decision.reason,
                        decision.conflicts_with,
                    ),
                )
        return StoredPlan(
            id=plan_id,
            source_sha256=plan.source_sha256,
            source_length=plan.source_length,
            ruleset_id=ruleset_id,
            plan_json=plan.to_json(),
            edit_count=plan.edit_count,
            candidates_total=result.candidates_total,
            candidates_dropped=result.candidates_dropped,
        )

    def get_plan(self, plan_id: str) -> StoredPlan:
        row = self.db.conn.execute(
            "SELECT * FROM plans WHERE id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise PlanNotFound("no such plan", plan_id=plan_id)
        return StoredPlan(
            id=row["id"],
            source_sha256=row["source_sha256"],
            source_length=int(row["source_length"]),
            ruleset_id=row["ruleset_id"],
            plan_json=row["plan_json"],
            edit_count=int(row["edit_count"]),
            candidates_total=int(row["candidates_total"]),
            candidates_dropped=int(row["candidates_dropped"]),
        )

    def get_decisions(self, plan_id: str) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM plan_decisions WHERE plan_id=? ORDER BY ordinal", (plan_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- applications
    def record_application(
        self,
        application_id: str,
        plan_id: str,
        source_id: str,
        expected_sha256: str,
        result_sha256: str,
        result_length: int,
        chunks_emitted: int,
        new_source_id: str | None,
    ) -> StoredApplication:
        try:
            with self.db.transaction() as cur:
                cur.execute(
                    "INSERT INTO applications(id,plan_id,source_id,expected_sha256,"
                    "result_sha256,result_length,new_source_id,chunks_emitted) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        application_id,
                        plan_id,
                        source_id,
                        expected_sha256,
                        result_sha256,
                        result_length,
                        new_source_id,
                        chunks_emitted,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AlreadyAppliedError(
                "this plan has already been applied", plan_id=plan_id
            ) from exc
        return StoredApplication(
            id=application_id,
            plan_id=plan_id,
            source_id=source_id,
            expected_sha256=expected_sha256,
            result_sha256=result_sha256,
            result_length=result_length,
            new_source_id=new_source_id,
            chunks_emitted=chunks_emitted,
        )

    def get_application_by_plan(self, plan_id: str) -> StoredApplication | None:
        row = self.db.conn.execute(
            "SELECT * FROM applications WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            return None
        return StoredApplication(
            id=row["id"],
            plan_id=row["plan_id"],
            source_id=row["source_id"],
            expected_sha256=row["expected_sha256"],
            result_sha256=row["result_sha256"],
            result_length=int(row["result_length"]),
            new_source_id=row["new_source_id"],
            chunks_emitted=int(row["chunks_emitted"]),
        )
