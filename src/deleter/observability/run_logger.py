"""运行编号与可重放测试日志。

每个被服务处理的操作都有一个 run_id，落盘为 ``runs/<run_id>.json``：
    - run_id, 时间戳, 接口, 输入摘要, 响应/错误（含错误类别）
    - kernel_trace: 内核每个关键中间判断（命中证据、越界保留、失效分类）
    - state_after: 操作后关键状态（序列号水位、live 文件版本、已注册删除）

另外维护 ``runs/index.jsonl``，每个 run 一行摘要，供"按编号重放问题"检索。
所有时间戳为 UTC ISO-8601。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id(endpoint: str) -> str:
    # 前缀可读，后段 uuid 保证唯一、可排序（时间在前）
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run-{ts}-{endpoint.strip('/').replace('/', '-') or 'root'}-{uuid.uuid4().hex[:8]}"


class RunLogger:
    """线程安全的 run 落盘记录器。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.runs_dir = Path(root) / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.runs_dir / "index.jsonl"
        self._lock = threading.Lock()

    def write_run(
        self,
        run_id: str,
        endpoint: str,
        request_summary: dict[str, Any],
        response_summary: dict[str, Any] | None = None,
        error_summary: dict[str, Any] | None = None,
        kernel_trace: list[dict[str, Any]] | None = None,
        state_after: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        http_status: int | None = None,
    ) -> Path:
        record = {
            "run_id": run_id,
            "ts": utc_now_iso(),
            "endpoint": endpoint,
            "http_status": http_status,
            "request": request_summary,
            "response": response_summary,
            "error": error_summary,
            "kernel_trace": kernel_trace or [],
            "state_after": state_after or {},
            "duration_ms": duration_ms,
        }
        path = self.runs_dir / f"{run_id}.json"
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            with self.index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "run_id": run_id, "ts": record["ts"], "endpoint": endpoint,
                    "http_status": http_status,
                    "error_category": (error_summary or {}).get("category"),
                }, ensure_ascii=False) + "\n")
        return path

    def read_run(self, run_id: str) -> dict[str, Any]:
        path = self.runs_dir / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(run_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        out = [json.loads(line) for line in lines[-limit:]]
        return list(reversed(out))

    def trace_collector(self) -> tuple[list[dict[str, Any]], Any]:
        """返回 (trace 列表, 回调)，传给内核 evaluate_table。"""
        events: list[dict[str, Any]] = []
        return events, events.append
