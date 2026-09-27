"""请求追踪器：把一次请求的身份、版本、步骤、失败原因聚合成一条 trace。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_request_id() -> str:
    """短而唯一的请求身份；客户端也可用 X-Request-ID 指定。"""
    return uuid.uuid4().hex[:16]


@dataclass
class RequestTracer:
    request_id: str
    started_at: float = field(default_factory=time.time)
    expression: str | None = None
    version: int | None = None
    status: str = "ok"
    error_category: str | None = None
    error_message: str | None = None
    result_count: int | None = None
    stats: dict | None = None
    steps: list[dict] = field(default_factory=list)

    def finish(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "started_at": self.started_at,
            "finished_at": time.time(),
            "expression": self.expression,
            "version": self.version,
            "status": self.status,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "result_count": self.result_count,
            "stats": self.stats,
            "steps": self.steps,
        }
