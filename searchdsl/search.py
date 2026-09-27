"""Query orchestration: parse -> validate -> normalize -> store -> execute.

This is the single entry point used by both the HTTP service and the CLI,
so behavior and diagnostics are identical across them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from searchdsl.astnodes import canonical_hash, canonical_json
from searchdsl.config import Config
from searchdsl.diagnostics import RunContext, env_run_id
from searchdsl.errors import SearchDSLError
from searchdsl.executor import EvalResult, execute
from searchdsl.normalize import normalize
from searchdsl.parser import parse
from searchdsl.spec import Schema, load_schema
from searchdsl.store import Store
from searchdsl.validate import validate


@dataclass
class SearchResponse:
    run_id: str
    status: str
    query: str
    canonical: Optional[dict]
    query_hash: Optional[str]
    total: int
    limit: int
    offset: int
    results: list[dict]
    explain: Optional[dict]
    budget: Optional[dict]
    versions: dict
    diagnostics: dict
    error: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "query": self.query,
            "canonical": self.canonical,
            "query_hash": self.query_hash,
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "results": self.results,
            "explain": self.explain,
            "budget": self.budget,
            "versions": self.versions,
            "diagnostics": self.diagnostics,
            "error": self.error,
        }


class SearchEngine:
    """Owns the schema, versioned store and configuration."""

    def __init__(self, config: Config, *, store: Optional[Store] = None,
                 schema: Optional[Schema] = None):
        self.config = config
        self.schema = schema or load_schema(config.paths.schema)
        # An injected store still gets built/rebuilt from the configured
        # corpus when versions are missing or stale (important for
        # ":memory:" stores, which always start empty).
        self.store = store if store is not None else Store(config.paths.database)
        self.store.build_from_files(config.paths.schema, config.paths.corpus)
        self.version = self.store.versions()

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def parse_canonical(self, query: str, ctx: RunContext):
        """parse -> budget/size guard -> validate -> normalize."""
        raw_bytes = len(query.encode("utf-8"))
        ctx.event("input", "received query", bytes=raw_bytes)
        if raw_bytes > self.config.limits.max_query_bytes:
            from searchdsl.errors import make_error

            raise make_error(
                "QUERY_TOO_LONG",
                f"query is {raw_bytes} bytes, budget is "
                f"{self.config.limits.max_query_bytes}",
            )
        tree = parse(query)
        report = validate(tree, self.schema, self.config.limits)
        canonical = normalize(tree)

        ctx.progress("parse", 1, 4, "tokenizing and building parse tree",
                     detail={"tree": canonical_json(tree)})
        ctx.progress("validate", 2, 4,
                     "checking field whitelist, types, budget",
                     detail={"budget": report.as_dict()})
        ctx.progress("normalize", 3, 4, "rewriting to canonical form",
                     detail={"tree": canonical_json(canonical)})
        return canonical, report

    def search(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        offset: int = 0,
        explain: bool = False,
        save: bool = True,
        run_id: Optional[str] = None,
    ) -> SearchResponse:
        run_id = run_id or env_run_id()
        ctx = RunContext(
            query=query,
            run_id=run_id,
            versions=self.version.as_dict() if self.version else {},
        )
        lim = limit or self.config.search.default_limit
        lim = min(lim, self.config.search.max_limit)
        versions = self.version.as_dict() if self.version else {}

        try:
            if offset < 0:
                raise SearchDSLError("offset must be >= 0", code="VALUE_MALFORMED")
            if lim <= 0:
                raise SearchDSLError("limit must be > 0", code="VALUE_MALFORMED")
            if offset + lim > self.config.limits.max_result_window:
                raise SearchDSLError(
                    f"offset+limit {offset + lim} exceeds result window "
                    f"{self.config.limits.max_result_window}",
                    code="BUDGET_RESULT_WINDOW",
                )

            canonical, report = self.parse_canonical(query, ctx)
            qhash = canonical_hash(canonical)
            if save:
                inserted = self.store.save_query(
                    qhash, canonical_json(canonical), source=query, version=self.version
                )
                ctx.event("store", "canonical query recorded",
                          query_hash=qhash, inserted=inserted)

            result: EvalResult = execute(canonical, self.store, self.schema)
            ordered = result.ordered()
            page_ids = ordered[offset:offset + lim]
            results = []
            for doc_id in page_ids:
                doc = self.store.get_doc(doc_id)
                item = {"doc_id": doc_id, "score": result.scores.get(doc_id, 0)}
                if doc is not None:
                    item["fields"] = doc.get("fields", {})
                results.append(item)

            ctx.progress("execute", 4, 4, "evaluating tree against index",
                         detail={"total": len(ordered), "returned": len(results)})

            return SearchResponse(
                run_id=run_id,
                status="ok",
                query=query,
                canonical=canonical.to_canonical(),
                query_hash=qhash,
                total=len(ordered),
                limit=lim,
                offset=offset,
                results=results,
                explain=result.steps.as_dict() if explain else None,
                budget=report.as_dict(),
                versions=versions,
                diagnostics={"summary": ctx.summary(), "events": ctx.events},
            )
        except SearchDSLError as exc:
            ctx.fail(
                exc.code,
                exc.message,
                stage="validate" if exc.code not in {"QUERY_EMPTY", "QUERY_TOO_LONG"}
                and exc.code not in _SYNTAX_CODES else "parse",
                pos=exc.pos.as_dict() if exc.pos else None,
                detail=exc.detail,
            )
            return SearchResponse(
                run_id=run_id,
                status="error",
                query=query,
                canonical=None,
                query_hash=None,
                total=0,
                limit=lim,
                offset=offset,
                results=[],
                explain=None,
                budget=None,
                versions=versions,
                diagnostics={"summary": ctx.summary(), "events": ctx.events},
                error=exc.as_dict(),
            )


_SYNTAX_CODES = {
    "UNTERMINATED_STRING",
    "UNTERMINATED_ESCAPE",
    "UNBALANCED_PAREN",
    "UNEXPECTED_TOKEN",
    "RANGE_MALFORMED",
    "RANGE_EMPTY",
}
