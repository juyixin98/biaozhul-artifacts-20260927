"""运行记录服务：为每次调用生成 run_id、记录分阶段中间状态并持久化，便于重放问题。"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any

from app.errors import AppError
from app.metadata.store import Store


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex}"


class RunRecorder:
    def __init__(self, store: Store | None):
        # 建表前 store 尚不存在时也允许使用（日志降级为仅返回 run_id）
        self.store = store
        self.phases: list[dict[str, Any]] = []

    def add(self, phase: str, detail: dict[str, Any], reason: str | None = None) -> None:
        entry: dict[str, Any] = {"phase": phase, "detail": detail}
        if reason:
            entry["reason"] = reason
        self.phases.append(entry)

    @contextmanager
    def record(self, *, kind: str, table_id: str | None, request: dict[str, Any]):
        run_id = new_run_id()
        started = time.perf_counter()
        status = "OK"
        error_env: dict[str, Any] | None = None
        try:
            yield run_id, self
        except AppError as exc:
            status = "ERROR"
            error_env = {"category": exc.category, "code": exc.code,
                         "message": exc.message, "details": exc.details}
            self.add("error", error_env, reason=f"{exc.category}/{exc.code}")
            raise
        except Exception as exc:  # 未预期异常归一化为 COMPUTATION_FAILED
            from app.errors import ComputationFailed

            status = "ERROR"
            wrapped = ComputationFailed("INTERNAL_ERROR", str(exc), {"type": type(exc).__name__})
            error_env = {"category": wrapped.category, "code": wrapped.code,
                         "message": wrapped.message, "details": wrapped.details}
            self.add("error", error_env, reason="COMPUTATION_FAILED/INTERNAL_ERROR")
            raise wrapped from exc
        finally:
            if self.store is not None:
                try:
                    self.store.insert_request_log(
                        run_id=run_id, kind=kind, table_id=table_id, status=status,
                        request=request, phases=self.phases, error=error_env,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                except Exception:
                    # 日志失败不掩盖业务结果
                    pass
