"""审计门面：为每个外部请求分配请求身份并贯穿所有处理位置。"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from .models import FailureKind
from .store import Store, failure_status


class Auditor:
    COMPONENTS = ("api", "parser", "crypto_verify", "kernel", "universe",
                  "evidence", "diffengine", "store")

    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def new_request_id() -> str:
        return "req_" + uuid.uuid4().hex

    def event(
        self,
        *,
        request_id: str,
        actor: str,
        component: str,
        stage: str,
        status: str = "OK",
        version: Optional[str] = None,
        diff_id: Optional[str] = None,
        summary: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> int:
        return self.store.append_audit(
            request_id=request_id,
            actor=actor,
            component=component,
            stage=stage,
            status=status,
            version=version,
            diff_id=diff_id,
            summary=summary,
            detail=detail,
        )

    def failure(
        self,
        *,
        request_id: str,
        actor: str,
        component: str,
        stage: str,
        kind: FailureKind,
        message: str,
        diff_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> int:
        merged = {"kind": kind.value, "message": message}
        if detail:
            merged.update(detail)
        return self.event(
            request_id=request_id,
            actor=actor,
            component=component,
            stage=stage,
            status=failure_status(kind),
            diff_id=diff_id,
            summary=message,
            detail=merged,
        )

    def inconclusive(
        self,
        *,
        request_id: str,
        actor: str,
        component: str,
        stage: str,
        count: int,
        diff_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> int:
        merged = {"unknown_points": count}
        if detail:
            merged.update(detail)
        return self.event(
            request_id=request_id,
            actor=actor,
            component=component,
            stage=stage,
            status="INCONCLUSIVE",
            diff_id=diff_id,
            summary=f"{count} 个空间点判定不确定（UNKNOWN），未按允许处理",
            detail=merged,
        )

    def query(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.store.query_audit(**kwargs)
