"""执行引擎：parse -> validate -> budget -> normalize -> execute -> register。

每个阶段的成功与失败都写入诊断日志（带 run_id 与判定依据）。
任何阶段失败：写 status=failed 事件后原样抛出 DslError，
绝不把异常伪装成成功结果。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import ast_nodes as ast
from .budget import enforce as budget_enforce
from .config import Settings
from .diagnostics import Diagnostics, new_run_id
from .errors import DslError, ErrorCategory
from .index import Index
from .normalize import is_idempotent, normalize
from .parser import parse
from .store import Store


@dataclass
class QueryResult:
    run_id: str
    query: str
    canonical: dict
    version: str
    matches: List[str]
    count: int
    budget_usage: dict
    idempotent: bool
    duration_ms: float
    truncated: bool = field(default=False)


class Engine:
    def __init__(self, settings: Settings, store: Store,
                 index: Index, diagnostics: Diagnostics) -> None:
        self.settings = settings
        self.store = store
        self.index = index
        self.diag = diagnostics

    def run(self, query: str, limit: Optional[int] = None) -> QueryResult:
        run_id = new_run_id()
        limit = limit or self.settings.default_result_limit
        started = time.perf_counter()
        try:
            return self._pipeline(run_id, query, limit, started)
        except DslError as exc:
            self.diag.emit(run_id, "failed", "failed", {
                "query": query,
                "error": exc.to_dict(),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            })
            raise

    def _pipeline(self, run_id: str, query: str, limit: int, started: float) -> QueryResult:
        tree = parse(query)
        self.diag.emit(run_id, "parse", "ok", {
            "query": query, "node_type": type(tree).__name__,
        })

        self.settings.schema.validate(tree)
        self.diag.emit(run_id, "validate", "ok", {
            "fields_whitelist": self.settings.schema.fields,
        })

        usage = budget_enforce(tree, self.settings.budget)
        self.diag.emit(run_id, "budget", "ok", {
            "depth": usage.depth, "clauses": usage.clauses,
            "max_depth": self.settings.budget.max_depth,
            "max_clauses": self.settings.budget.max_clauses,
        })

        canonical_tree = normalize(tree)
        idem = is_idempotent(tree)
        canonical = ast.to_dict(canonical_tree)
        self.diag.emit(run_id, "normalize", "ok", {
            "canonical": canonical, "idempotent": idem,
        })
        if not idem:
            raise DslError(  # 规范化必须幂等，否则是实现缺陷
                ErrorCategory.PARSE,
                "内部错误：规范化不幂等",
            )

        matches = sorted(self.index.execute(canonical_tree))
        version = self.store.register_query(canonical)
        truncated = len(matches) > limit
        shown = matches[:limit]
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        self.diag.emit(run_id, "execute", "ok", {
            "version": version,
            "matched": len(matches),
            "returned": len(shown),
            "truncated": truncated,
            "duration_ms": duration_ms,
        })

        return QueryResult(
            run_id=run_id,
            query=query,
            canonical=canonical,
            version=version,
            matches=shown,
            count=len(matches),
            budget_usage={"depth": usage.depth, "clauses": usage.clauses},
            idempotent=idem,
            duration_ms=duration_ms,
            truncated=truncated,
        )
