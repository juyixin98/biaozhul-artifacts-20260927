"""Structured JSON-ish logging.

Every ingest step emits one log line that can be correlated to an input/run via
``run_id`` and ``seq``, and states the verdict explicitly. Exceptions and
unknown states are logged as ``error``/``rejected``, never collapsed to success.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("run_id", "seq", "validator", "step", "status",
                    "reject_reason", "evidence_id", "offense", "detail",
                    "version", "epoch", "chain_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def configure_logging(log_dir: str | Path | None = "logs",
                      level: int = logging.INFO) -> tuple[logging.Logger, str, Path | None]:
    logger = logging.getLogger("ffg_slash")
    logger.setLevel(level)
    logger.handlers.clear()
    run_id = new_run_id()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonLineFormatter())
    logger.addHandler(stream)

    log_path: Path | None = None
    if log_dir is not None:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path / f"run-{run_id}.log", encoding="utf-8")
        fh.setFormatter(JsonLineFormatter())
        logger.addHandler(fh)
    logger.propagate = False
    return logger, run_id, log_path


def get_logger() -> logging.Logger:
    return logging.getLogger("ffg_slash")
