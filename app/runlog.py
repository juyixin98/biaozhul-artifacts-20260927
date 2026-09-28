"""结构化运行日志。

每次“运行”（API 进程、pytest 会话、离线回放）都有一个 run_id，
每条日志记录：
    ts / run_id / stage / event / 输入或运行身份标识 /
    关键步骤与中间量 / 判定结果(verdict) / 错误类别。
未知/异常状态显式记为 error，绝不记 success。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

_run_id = os.environ.get("ABI_RUN_ID") or f"run-{uuid.uuid4().hex[:12]}"
_lock = threading.Lock()


def run_id() -> str:
    return _run_id


def _versions() -> dict:
    import platform

    versions = {"python": platform.python_version()}
    try:
        import Crypto  # pycryptodome
        versions["pycryptodome"] = Crypto.__version__
    except Exception:
        versions["pycryptodome"] = "unavailable"
    try:
        import eth_abi
        versions["eth_abi"] = eth_abi.__version__
    except Exception:
        versions["eth_abi"] = "unavailable"
    try:
        import fastapi
        versions["fastapi"] = fastapi.__version__
    except Exception:
        versions["fastapi"] = "unavailable"
    return versions


class RunLogger:
    def __init__(self, log_dir: str, stage: str):
        self.stage = stage
        self.run_id = _run_id
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{self.run_id}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8")
        # 可重入锁：close() 持锁时还要调用 _write。
        self._lock = threading.RLock()
        self.event_count = 0
        self._write(
            "run_start",
            identity={"pid": os.getpid(), "argv": sys.argv[:4]},
            versions=_versions(),
            verdict="started",
        )

    def _write(self, event: str, *, identity=None, steps=None, verdict: str = "ok",
               category: str | None = None, **extra) -> None:
        rec = {
            "ts": round(time.time(), 6),
            "run_id": self.run_id,
            "stage": self.stage,
            "event": event,
            "identity": identity,
            "steps": steps,
            "verdict": verdict,
            "category": category,
        }
        rec.update({k: v for k, v in extra.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            self.event_count += 1

    def step(self, event: str, *, identity=None, steps=None, **extra) -> None:
        """正常计算步骤/判定通过。"""
        self._write(event, identity=identity, steps=steps, verdict="ok", **extra)

    def failure(self, event: str, *, category: str, message: str,
                identity=None, steps=None, **extra) -> None:
        """明确失败；category 为稳定错误类别。绝不与 success 混淆。"""
        self._write(
            event, identity=identity, steps=steps,
            verdict="failure", category=category, message=message, **extra
        )

    def error(self, event: str, *, message: str, identity=None, **extra) -> None:
        """未知/内部异常。"""
        self._write(
            event, identity=identity, verdict="error",
            category="internal_error", message=message, **extra
        )

    def close(self, *, verdict: str = "completed", summary: dict | None = None) -> None:
        with self._lock:
            self._write(
                "run_end", verdict=verdict, summary=summary,
                events=self.event_count,
            )
            self._fh.close()


def get_logger(stage: str, log_dir: str | None = None) -> RunLogger:
    from .config import settings

    return RunLogger(log_dir or settings.log_dir, stage)
