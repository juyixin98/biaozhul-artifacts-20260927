"""运行日志：每次切段写一条 JSONL，带可重放的 run_id 与关键中间状态。

失败时记录失败类别（input/state/resource/computation/internal）、错误码、
作业参数与内核 snapshot/事件——日志本身即为“重放问题”所需的输入线索
（结合保存的原始字节即可重跑）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class RunLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def new_run_id() -> str:
        return f"run-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        """追加一条日志；自动补时间戳。返回写入的完整字典。"""
        payload = {"ts": time.time(), **entry}
        line = json.dumps(payload, ensure_ascii=False, default=_json_default)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return payload

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def find_run(self, run_id: str) -> dict[str, Any] | None:
        for entry in self.read_all():
            if entry.get("run_id") == run_id:
                return entry
        return None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (set, tuple)):
        return list(obj)
    return repr(obj)
