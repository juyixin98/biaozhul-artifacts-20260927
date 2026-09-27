"""Structured, run-correlated diagnostics.

A :class:`RunContext` carries a unique run id (override for tests), the
input query, and the active protocol versions. Every recorded event is a
JSON-serializable dict with a monotonic sequence number, timestamp,
``stage`` and ``level``. Errors are recorded as ``level=error`` with the
stable taxonomy code — the run is marked failed and nothing collapses an
exception into success (``status`` stays ``error``).

The same context feeds both the API response (``diagnostics`` field) and
the log file written by ``searchdsl diagnose`` / the test harness.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from searchdsl import DSL_SPEC_VERSION, INDEX_SCHEMA_VERSION, __version__


def new_run_id() -> str:
    return f"run-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


@dataclass
class RunContext:
    query: str
    run_id: str = field(default_factory=new_run_id)
    versions: dict[str, str] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    status: str = "ok"
    error_code: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.versions = {
            "package_version": __version__,
            "dsl_version": DSL_SPEC_VERSION,
            "index_schema_version": INDEX_SCHEMA_VERSION,
            **self.versions,
        }

    def event(
        self,
        stage: str,
        message: str,
        *,
        level: str = "info",
        **detail: Any,
    ) -> dict:
        evt = {
            "seq": len(self.events) + 1,
            "ts_ms": round((time.time() - self.started_at) * 1000, 3),
            "run_id": self.run_id,
            "query": self.query,
            "stage": stage,
            "level": level,
            "message": message,
        }
        if detail:
            evt["detail"] = detail
        self.events.append(evt)
        return evt

    def progress(self, stage: str, current: int, total: int, message: str = "", **detail):
        return self.event(
            stage,
            message or f"{stage} {current}/{total}",
            current=current,
            total=total,
            **detail,
        )

    def fail(self, code: str, message: str, *, stage: str = "runtime", **detail) -> dict:
        self.status = "error"
        self.error_code = code
        return self.event(stage, message, level="error", code=code, **detail)

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "query": self.query,
            "status": self.status,
            "error_code": self.error_code,
            "versions": self.versions,
            "event_count": len(self.events),
        }

    def as_dict(self) -> dict:
        return {**self.summary(), "events": self.events}

    def to_jsonl(self) -> str:
        lines = [
            json.dumps({"type": "summary", **self.summary()}, ensure_ascii=False, sort_keys=True)
        ]
        lines.extend(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in self.events)
        return "\n".join(lines) + "\n"

    def write_jsonl(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_jsonl(), encoding="utf-8")
        return p


def env_run_id() -> str:
    """Run id from SEARCHDSL_RUN_ID when set (used by the test harness)."""
    return os.environ.get("SEARCHDSL_RUN_ID", new_run_id())
