"""结构化 JSONL 日志。

服务端写操作、查询剪枝统计、错误都会落 JSONL；测试另有自己的 journal
（见 tests/conftest.py）。每行带 request_id / run 身份，便于关联输入。
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            + f".{int(record.created * 1000) % 1000:03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(log_dir: Path, level: str = "INFO", *, run_id: str | None = None) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or uuid.uuid4().hex[:12]
    path = log_dir / f"service-{time.strftime('%Y%m%d', time.gmtime())}.jsonl"
    logger = logging.getLogger("trie_service")
    logger.setLevel(level)
    logger.handlers.clear()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(JsonlFormatter())
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False
    logger.info("logging configured", extra={"data": {"run_id": run_id, "file": str(path)}})
    return logger, path
