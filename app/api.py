"""FastAPI wiring: request identity, explainable payloads, structured logs."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .errors import InvalidParameterError, ServiceError
from .service import SpellcheckService
from .storage import VersionStore

logger = logging.getLogger("spellcheck")


class CorrectRequest(BaseModel):
    query: str = Field(..., description="Raw query text; normalized server-side")
    threshold: float | None = Field(None, ge=0)
    max_results: int | None = Field(None, ge=1)
    version_id: int | None = None
    include_paths: bool = True


def _serialize(service_result, settings: Settings) -> dict:
    tokens_out = []
    for tr in service_result.token_reports:
        tokens_out.append(
            {
                "token": tr.token.text,
                "offset": [tr.token.start, tr.token.end],
                "exact_match": tr.exact_match,
                "stage": tr.stage,
                "candidates": [
                    {
                        "candidate": c.candidate,
                        "distance": round(c.distance, 9),
                        "frequency": c.frequency,
                        "path": c.path,
                        "path_recomputed_cost": round(c.path_recomputed_cost, 9),
                        "path_replay_verified": c.path_matches,
                    }
                    for c in tr.corrections
                ],
            }
        )
    return {
        "request_id": service_result.request_id,
        "dictionary": {
            "version_id": service_result.version_id,
            "is_active_version": service_result.version_active,
        },
        "algorithm": {
            "variant": (
                "weighted adjacent-transposition edit distance "
                "(unrestricted Damerau semantics via A* shortest path)"
            ),
            "costs_are_non_negative": True,
        },
        "normalized_query": service_result.normalized_query,
        "threshold": service_result.threshold,
        "corrected_text": service_result.corrected_text,
        "tokens": tokens_out,
        "uncertainties": service_result.uncertainties,
        "limits": {
            "max_query_chars": settings.limits.max_query_chars,
            "max_query_tokens": settings.limits.max_query_tokens,
            "max_candidates_scored": settings.limits.max_candidates_scored,
            "max_results_per_token": settings.limits.max_results_per_token,
            "max_search_nodes": settings.limits.max_search_nodes,
        },
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = VersionStore(settings.db_path)
    service = SpellcheckService(settings, store)
    app = FastAPI(
        title="Weighted Edit-Distance Spell Correction",
        version="1.0.0",
        description=(
            "Candidate correction with weighted insertion, deletion, "
            "substitution and adjacent-transposition costs (unrestricted "
            "Damerau semantics via A* shortest path)."
        ),
    )
    app.state.settings = settings
    app.state.service = service

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        rid = getattr(request.state, "request_id", "-")
        logger.warning(
            "request failed",
            extra={"request_id": rid, "error_code": exc.code, "details": exc.details},
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "request_id": rid,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            },
        )

    @app.get("/health")
    async def health(request: Request):
        active = store.active_version()
        return {
            "request_id": request.state.request_id,
            "status": "ok",
            "active_version_id": active,
            "variant": (
                "weighted adjacent-transposition edit distance "
                "(unrestricted Damerau, A* shortest path)"
            ),
        }

    @app.post("/correct")
    async def correct(body: CorrectRequest, request: Request):
        rid = request.state.request_id
        logger.info(
            "correction query",
            extra={"request_id": rid, "raw_query": body.query, "version": body.version_id},
        )
        if body.max_results is not None and body.max_results > settings.limits.max_results_per_token:
            raise InvalidParameterError(
                f"max_results must be <= {settings.limits.max_results_per_token}",
                details={"limit": settings.limits.max_results_per_token},
            )
        result = service.correct(
            body.query,
            request_id=rid,
            threshold=body.threshold,
            max_results=body.max_results,
            version_id=body.version_id,
            include_paths=body.include_paths,
        )
        payload = _serialize(result, settings)
        logger.info(
            "correction done",
            extra={
                "request_id": rid,
                "tokens": len(payload["tokens"]),
                "uncertainties": len(payload["uncertainties"]),
            },
        )
        return payload

    @app.get("/versions")
    async def versions(request: Request):
        return {
            "request_id": request.state.request_id,
            "active_version_id": store.active_version(),
            "versions": store.list_versions(),
        }

    @app.get("/diagnostics")
    async def diagnostics(request: Request):
        """Static description of where/what, plus live version info."""
        return {
            "request_id": request.state.request_id,
            "algorithm": {
                "variant": (
                    "weighted adjacent-transposition edit distance "
                    "(unrestricted Damerau semantics)"
                ),
                "method": "A* over string states with admissible f-pruning",
                "operations": ["insert", "delete", "substitute",
                               "adjacent transpose (ordered-pair cost)"],
                "lowrance_wagner_table_recurrence_used": False,
                "restricted_OSA_used": False,
            },
            "pruning": {
                "dictionary_bounds": [
                    "directional length (min_delete/min_insert)",
                    "0.5*L1(freq)*min_edit",
                ],
                "search_heuristic": "max of the same two bounds per state",
                "requirement": "non-negative costs",
                "lossless_against_threshold": True,
            },
            "ranking_key": "(distance asc, term asc, frequency desc)",
            "path_replay": "ordered steps applied sequentially; each precondition checked; cost recomputed independently",
            "costs": {
                "defaults": {
                    "insert": settings.costs.insert_default,
                    "delete": settings.costs.delete_default,
                    "substitute": settings.costs.substitute_default,
                    "transpose": settings.costs.transpose_default,
                },
                "min_indel": settings.costs.min_indel,
                "min_edit": settings.costs.min_edit,
            },
            "db_path": str(Path(settings.db_path).resolve()),
            "active_version_id": store.active_version(),
        }

    return app


app = create_app()
