"""结构化 JSONL 日志。

每行一个 JSON 对象，字段稳定，便于事后 grep/jq 复现失败：
- request_id 贯穿 HTTP 响应、SQLite request_traces 与日志文件；
- event 区分 request_start / request_done / error；
- 任何失败单列 category 与 message，绝不与正常结果混排。

实现上直接在锁保护下追加写文件，而不使用全局 logging.Handler——
同一进程里多次 create_app（如测试）时，全局 handler 会把日志写到
第一个实例的目录，直接写文件可保证“哪个库/哪个日志目录”一一对应。
"""
from __future__ import annotations

import json
import pathlib
import threading
import time


class JsonlLogger:
    def __init__(self, log_dir: str) -> None:
        self.log_dir = pathlib.Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / "service.jsonl"
        self._lock = threading.Lock()

    def _emit(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()

    def request_start(self, request_id: str, method: str, path: str, qs: str) -> None:
        self._emit(
            {
                "ts": time.time(),
                "event": "request_start",
                "request_id": request_id,
                "method": method,
                "path": path,
                "query": qs,
            }
        )

    def request_done(
        self,
        request_id: str,
        *,
        status: str,
        version: int | None,
        expression: str | None,
        result_count: int | None,
        error_category: str | None = None,
        error_message: str | None = None,
        stats: dict | None = None,
        short_circuited: bool | None = None,
    ) -> None:
        payload = {
            "ts": time.time(),
            "event": "request_done",
            "request_id": request_id,
            "status": status,
            "version": version,
            "expression": expression,
            "result_count": result_count,
        }
        if error_category:
            payload["error_category"] = error_category
        if error_message:
            payload["error_message"] = error_message
        if stats is not None:
            payload["stats"] = stats
        if short_circuited is not None:
            payload["short_circuited"] = short_circuited
        self._emit(payload)

    def info(self, request_id: str, event: str, **fields) -> None:
        payload = {
            "ts": time.time(),
            "event": event,
            "request_id": request_id,
        }
        payload.update(fields)
        self._emit(payload)
