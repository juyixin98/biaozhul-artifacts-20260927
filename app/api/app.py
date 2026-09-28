"""FastAPI application factory and routes.

Run locally::

    NRS_DB=/tmp/nrs.db .venv/bin/uvicorn app.api.app:app --reload

The HTTP layer is deliberately thin: translate JSON -> service calls -> JSON,
and map :class:`~app.errors.AppError` categories to stable status codes.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..errors import AppError
from ..storage import Database, Repository
from .schemas import (
    ApplyIn,
    ApplyOut,
    EditOut,
    PlanDetailOut,
    PlanOut,
    RulesetIn,
    RulesetOut,
    SourceIn,
    SourceOut,
)
from .service import Service, ServiceConfig


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Non-overlapping Replacement Planning Service",
        version=__version__,
        description=(
            "Plans and applies non-overlapping regex rewrites over large "
            "UTF-8 text using the linear-time RE2 engine and restricted "
            "capture templates."
        ),
    )
    db_path = db_path or os.environ.get("NRS_DB", ":memory:")
    database = Database(db_path)
    service = Service(Repository(database), ServiceConfig.from_env())
    app.state.db = database
    app.state.service = service

    # --------------------------------------------------------- error mapping
    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    # ---------------------------------------------------------------- health
    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "engine": "re2",
            "limits": {
                "max_source_bytes": service.config.max_source_bytes,
                "max_candidates_per_rule": service.config.max_candidates_per_rule,
                "max_edits": service.config.max_edits,
                "max_output_bytes": service.config.max_output_bytes,
            },
        }

    # --------------------------------------------------------------- sources
    @app.put("/sources/{source_id}", response_model=SourceOut, tags=["sources"])
    async def put_source(source_id: str, body: SourceIn) -> SourceOut:
        stored = service.upload_source(
            source_id, body.text, normalize_newlines=body.normalize_newlines
        )
        return SourceOut(
            id=stored.id,
            version=stored.version,
            sha256=stored.sha256,
            length=stored.length,
            normalize_newlines=stored.normalize_newlines,
        )

    @app.get("/sources/{source_id}", response_model=SourceOut, tags=["sources"])
    async def get_source(source_id: str) -> SourceOut:
        s = service.get_source(source_id)
        return SourceOut(
            id=s.id,
            version=s.version,
            sha256=s.sha256,
            length=s.length,
            normalize_newlines=s.normalize_newlines,
        )

    # --------------------------------------------------------------- rulesets
    @app.put("/rulesets/{ruleset_id}", response_model=RulesetOut, tags=["rulesets"])
    async def put_ruleset(ruleset_id: str, body: RulesetIn) -> RulesetOut:
        service.put_ruleset(ruleset_id, [r.model_dump() for r in body.rules])
        stored = service.repo.get_ruleset(ruleset_id)
        return RulesetOut(id=stored.id, version=stored.version, rule_count=len(stored.rules))

    # ------------------------------------------------------------------ plans
    @app.post(
        "/sources/{source_id}/plans/{ruleset_id}",
        response_model=PlanOut,
        tags=["plans"],
    )
    async def create_plan(source_id: str, ruleset_id: str) -> PlanOut:
        stored, _ = service.create_plan(source_id, ruleset_id)
        return PlanOut(
            plan_id=stored.id,
            source_sha256=stored.source_sha256,
            source_length=stored.source_length,
            ruleset_id=stored.ruleset_id,
            edit_count=stored.edit_count,
            candidates_total=stored.candidates_total,
            candidates_dropped=stored.candidates_dropped,
        )

    @app.get("/plans/{plan_id}", response_model=PlanDetailOut, tags=["plans"])
    async def get_plan(plan_id: str) -> PlanDetailOut:
        stored, plan, decisions = service.get_plan_detail(plan_id)
        return PlanDetailOut(
            plan_id=stored.id,
            source_sha256=stored.source_sha256,
            source_length=stored.source_length,
            ruleset_id=stored.ruleset_id,
            edit_count=stored.edit_count,
            candidates_total=stored.candidates_total,
            candidates_dropped=stored.candidates_dropped,
            edits=[
                EditOut(
                    start=e.start,
                    end=e.end,
                    zero_width=e.zero_width,
                    rule_id=e.rule_id,
                    matched=e.matched.decode("utf-8"),
                    replacement=e.replacement.decode("utf-8"),
                )
                for e in plan.edits
            ],
            decisions=decisions,
        )

    # ----------------------------------------------------------------- apply
    @app.post("/plans/{plan_id}/apply", response_model=ApplyOut, tags=["plans"])
    async def apply_plan(plan_id: str, body: ApplyIn) -> ApplyOut:
        record, output = service.apply_plan(
            plan_id,
            expected_sha256=body.expected_sha256,
            source_id=body.source_id,
            save_result_as=body.save_result_as,
        )
        return ApplyOut(
            application_id=record.id,
            plan_id=record.plan_id,
            source_id=record.source_id,
            expected_sha256=record.expected_sha256,
            result_sha256=record.result_sha256,
            result_length=record.result_length,
            chunks_emitted=record.chunks_emitted,
            new_source_id=record.new_source_id,
            output=output.decode("utf-8"),
        )

    return app


app = create_app()
