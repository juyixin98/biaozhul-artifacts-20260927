"""合并运行日志。

每个 run 一个 JSONL 文件，每行一个结构化事件，满足可核验要求：
- 关联输入与运行身份：run_id、表名、三方快照 ID（共同祖先/开发/主）；
- 显示版本与计算步骤：service_version、阶段事件（start/classify/resolve/commit）；
- 显示判定依据：每个记录键的分类、是否冲突、理由、字段来源；
- 不吞异常：失败事件带 status=failed、error.code 与 error.message，
  未知异常不会被记录成成功。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .. import __version__

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _short(snapshot_id: str | None, n: int = 12) -> str | None:
    return snapshot_id[:n] if snapshot_id else snapshot_id


class RunLogger:
    """线程安全的 JSONL 运行日志。同时可镜像到 stderr（演示/调试）。"""

    def __init__(self, log_dir: str | Path, run_id: str, echo: bool = False,
                 level: str = "debug"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"run-{run_id}.jsonl"
        self.run_id = run_id
        self.echo = echo
        self._min_level = _LEVELS[level]
        self._lock = threading.Lock()

    def event(self, stage: str, level: str = "info", **fields: Any) -> None:
        if _LEVELS[level] < self._min_level:
            return
        rec = {
            "ts": utc_now_iso(),
            "run_id": self.run_id,
            "service_version": __version__,
            "pid": os.getpid(),
            "stage": stage,
            "level": level,
        }
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if self.echo:  # pragma: no cover - 演示输出
                print(line, file=sys.stderr)

    def start(self, table: str, base: str, ours: str, theirs: str,
              ours_branch: str, theirs_branch: str) -> None:
        self.event(
            "merge_start",
            table=table,
            ours_branch=ours_branch,
            theirs_branch=theirs_branch,
            input_snapshots={
                "base_ancestor": base,
                "ours_develop": ours,
                "theirs_main": theirs,
            },
        )

    def plan_built(self, classifications: dict[str, str], conflicts: list[dict[str, Any]],
                   auto_count: int, conflict_count: int, include_detail: bool) -> None:
        self.event(
            "classify_done",
            step="按主键逐键三方分类",
            totals={"auto": auto_count, "conflict": conflict_count,
                    "total": auto_count + conflict_count},
            per_key=classifications if include_detail else None,
            conflicts=conflicts,
        )

    def resolved(self, key: str, classification: str, kind: str,
                 binding: tuple[str, str, str], detail: dict[str, Any]) -> None:
        self.event(
            "conflict_resolved",
            key=key,
            classification=classification,
            resolution=kind,
            bound_to_snapshots={
                "base": _short(binding[0]),
                "ours": _short(binding[1]),
                "theirs": _short(binding[2]),
            },
            detail=detail,
        )

    def rejected(self, key: str, reason: str, error_code: str) -> None:
        self.event(
            "resolution_rejected",
            level="warn",
            key=key,
            error={"code": error_code, "message": reason},
            status="failed",
        )

    def committed(self, snapshot_id: str, row_count: int, parents: list[str],
                  target_branch: str, old_head: str) -> None:
        self.event(
            "merge_committed",
            status="ok",
            merged_snapshot_id=snapshot_id,
            row_count=row_count,
            parent_refs=parents,
            branch_advanced={"branch": target_branch, "from": old_head, "to": snapshot_id},
        )

    def failed(self, stage: str, error_code: str, message: str) -> None:
        self.event(
            stage,
            level="error",
            status="failed",
            error={"code": error_code, "message": message},
        )


class NullRunLogger:
    """不需要落盘时（部分单元测试）使用的空实现。"""

    path = None
    run_id = None

    def event(self, *a: Any, **k: Any) -> None:
        return None

    def __getattr__(self, name: str):
        return lambda *a, **k: None
