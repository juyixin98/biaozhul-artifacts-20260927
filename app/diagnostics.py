"""诊断记录器：把规划与应用过程的关键中间状态结构化落库。

每条事件带运行编号 ``run_id``，用于重放：测试与线上问题可凭 run_id 拉取
完整中间状态（候选排序、被淘汰命中、渲染结果、摘要比对等）。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from .config import new_run_id
from .storage import Repository


class Diag:
    def __init__(self, repo: Repository, run_id: str | None = None):
        self.repo = repo
        self.run_id = run_id or new_run_id()

    def event(self, stage: str, level: str, event: str, message: str, **data: Any) -> None:
        self.repo.add_diag(self.run_id, stage, level, event, message, data)

    def info(self, stage: str, event: str, message: str, **data: Any) -> None:
        self.event(stage, "info", event, message, **data)

    def warn(self, stage: str, event: str, message: str, **data: Any) -> None:
        self.event(stage, "warn", event, message, **data)

    def error(self, stage: str, event: str, message: str, **data: Any) -> None:
        self.event(stage, "error", event, message, **data)

    @contextmanager
    def stage(self, name: str, **data: Any):
        self.info(name, "stage_start", f"enter stage {name}", **data)
        try:
            yield self
        except Exception as exc:  # noqa: BLE001
            self.error(
                name,
                "stage_failed",
                f"stage {name} failed: {type(exc).__name__}",
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
                message=str(exc),
            )
            raise
