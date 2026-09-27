"""FastAPI 服务：查询、文档与查询版本查询。

错误映射（绝不把异常统一返回成功）：
- LEXER_ERROR / PARSE_ERROR -> 400
- FIELD_UNKNOWN / FIELD_TYPE -> 422
- BUDGET_EXCEEDED           -> 413
错误响应携带 run_id，可与 logs/searchdsl.jsonl 中的诊断事件关联。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from searchdsl import __version__
from searchdsl.config import Settings, load_settings
from searchdsl.diagnostics import Diagnostics
from searchdsl.engine import Engine
from searchdsl.errors import DslError, ErrorCategory
from searchdsl.index import Index
from searchdsl.store import Store

_STATUS_BY_CATEGORY = {
    ErrorCategory.LEXER: 400,
    ErrorCategory.PARSE: 400,
    ErrorCategory.FIELD_UNKNOWN: 422,
    ErrorCategory.FIELD_TYPE: 422,
    ErrorCategory.BUDGET: 413,
}


class QueryRequest(BaseModel):
    query: str
    limit: Optional[int] = None


def create_app(settings: Optional[Settings] = None,
               fixtures_path: Optional[str | Path] = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.database_path)
    index = Index(store, settings.schema)
    diag = Diagnostics(settings.log_path)

    fixtures = Path(fixtures_path) if fixtures_path else (
        Path(__file__).resolve().parents[1] / "fixtures" / "documents.json"
    )
    with open(fixtures, "r", encoding="utf-8") as fh:
        documents = json.load(fh)
    index.rebuild(documents)

    engine = Engine(settings, store, index, diag)
    app = FastAPI(title="searchdsl", version=__version__)
    app.state.engine = engine
    app.state.store = store

    @app.exception_handler(DslError)
    async def dsl_error_handler(request: Request, exc: DslError) -> JSONResponse:
        status = _STATUS_BY_CATEGORY.get(exc.category, 400)
        return JSONResponse(status_code=status, content={"error": exc.to_dict()})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "searchdsl_version": __version__}

    @app.post("/query")
    def query(req: QueryRequest) -> dict:
        result = engine.run(req.query, limit=req.limit)
        return {
            "run_id": result.run_id,
            "query": result.query,
            "canonical": result.canonical,
            "version": result.version,
            "matches": result.matches,
            "count": result.count,
            "truncated": result.truncated,
            "budget_usage": result.budget_usage,
            "idempotent": result.idempotent,
            "duration_ms": result.duration_ms,
        }

    @app.get("/documents")
    def list_documents() -> dict:
        return {"documents": [dict(r) for r in store.all_documents()]}

    @app.get("/queries/{version_hash}")
    def get_query_version(version_hash: str) -> dict:
        row = store.get_query_version(version_hash)
        if row is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"category": "NOT_FOUND",
                                   "message": f"未知查询版本 {version_hash}",
                                   "position": None}},
            )
        return row

    return app


app = create_app()
