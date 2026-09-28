"""结构化日志：每行 JSON，带 request_id、步骤、版本/位置；失败与不确定性单列。"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .versions import version_bundle

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "versions": version_bundle(),
        }
        for key in ("request_id", "step", "location", "table", "reason",
                    "uncertain", "failure_category", "extra"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level=logging.INFO):
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("pruning")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str = "pruning"):
    configure_logging()
    return logging.getLogger(name)


def event(logger, level, request_id, step, message, *, location=None, table=None,
          reason=None, uncertain=None, failure_category=None, extra=None):
    logger.log(level, message, extra={
        "request_id": request_id, "step": step, "location": location,
        "table": table, "reason": reason, "uncertain": uncertain,
        "failure_category": failure_category, "extra": extra})
