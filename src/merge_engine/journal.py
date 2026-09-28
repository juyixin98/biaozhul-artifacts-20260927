"""运行日志：JSONL 追加事件流，保留足以重放问题的信息。

每个 run_id 一个文件 ``<journal_dir>/<run_id>.jsonl``，每行一个事件：

    run_started       输入摘要、配置、资源上限
    source_loaded     格式、行数、列、字节数、前若干行键
    snapshot_loaded   目标表、行数、rowid、指纹、前若干行键
    source_validated  重复键检测结论（冲突明细或 ok）
    null_policy       生效策略与 NULL 键行号
    target_validated  目标重复键检测结论
    plan_decided      动作摘要（类型/键/行号/目标rowid/理由）+ 计数
    plan_validated    资源统计
    commit_started    事务开始（注入点在此前记录）
    commit_failed     错误类别/code/细节（无部分更新将由重放方重新装载核对）
    run_rejected      决策期被拒绝的错误
    run_committed / dry_run_done

所有事件含 run_id、单调 seq、单调时钟时间戳、阶段名与“判断理由”。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .contracts import jsonable


class Journal:
    def __init__(self, directory: str | Path, *, clock=time.time) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()

    def path_for(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.jsonl"

    def event(self, run_id: str, phase: str, event: str,
              *, reason: str = "", **payload: Any) -> dict[str, Any]:
        entry = {
            "run_id": run_id,
            "ts": round(self._clock(), 6),
            "phase": phase,
            "event": event,
            "reason": reason,
            **jsonable(payload),
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=False)
        with self._lock:
            with self.path_for(run_id).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return entry

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        path = self.path_for(run_id)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events
