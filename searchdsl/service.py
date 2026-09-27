"""FastAPI service for the search DSL.

Endpoints
---------
GET  /health                       liveness + versions
GET  /schema                       field whitelist + default fields
GET  /versions                     package/dsl/index/corpus versions
GET  /searches/{query_hash}        fetch a previously stored canonical query
GET  /search?q=...&limit=&offset=&explain=
POST /search                       {"q": "...", "limit": 10, "offset": 0,
                                    "explain": true}

Errors are returned with HTTP 4xx and the stable taxonomy code in the
JSON body; they never come back as 200/"success".
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from searchdsl import DSL_SPEC_VERSION, INDEX_SCHEMA_VERSION, __version__
from searchdsl.config import Config, load_config
from searchdsl.search import SearchEngine


class SearchRequestBody(BaseModel):
    q: str = Field(..., description="search DSL query string")
    limit: Optional[int] = None
    offset: int = 0
    explain: bool = False


def create_app(config: Optional[Config] = None, *, engine: Optional[SearchEngine] = None):
    cfg = config or load_config()
    app = FastAPI(title="searchdsl", version=__version__)
    app.state.config = cfg
    app.state.engine = engine or SearchEngine(cfg)

    def _engine() -> SearchEngine:
        return app.state.engine

    @app.get("/health")
    def health():
        v = _engine().version
        return {
            "status": "ok",
            "versions": v.as_dict() if v else None,
            "package_version": __version__,
            "dsl_version": DSL_SPEC_VERSION,
            "index_schema_version": INDEX_SCHEMA_VERSION,
        }

    @app.get("/schema")
    def schema():
        return _engine().schema.as_dict()

    @app.get("/versions")
    def versions():
        v = _engine().version
        if v is None:
            raise HTTPException(503, "index not built")
        return v.as_dict()

    def _do_search(q: str, limit: Optional[int], offset: int, explain: bool):
        if q is None:
            raise HTTPException(status_code=400, detail={"code": "QUERY_EMPTY",
                                                        "message": "missing 'q' parameter"})
        resp = _engine().search(q, limit=limit, offset=offset, explain=explain)
        if resp.status != "ok":
            raise HTTPException(status_code=400, detail=resp.error)
        return resp.as_dict()

    @app.get("/search")
    def search_get(
        q: str = Query(..., description="search DSL query string"),
        limit: int = Query(10, ge=1),
        offset: int = Query(0, ge=0),
        explain: bool = False,
    ):
        return _do_search(q, limit, offset, explain)

    @app.post("/search")
    def search_post(body: SearchRequestBody):
        return _do_search(body.q, body.limit, body.offset, body.explain)

    @app.get("/searches/{query_hash}")
    def get_saved(query_hash: str):
        row = _engine().store.get_saved_query(query_hash)
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND",
                                                         "message": "unknown query hash"})
        return row

    return app


app = create_app()
