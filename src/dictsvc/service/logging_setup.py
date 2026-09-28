"""Per-run file logger.

Every run writes its own log file so that a failing test can correlate its
log to an input/run identity. Each log starts with version info and records
each kernel step plus the final verdict and its basis.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings


class RunLogger:
    def __init__(self, settings: Settings) -> None:
        self.log_dir = Path(settings.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, run_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_"
                       for c in run_id)
        return self.log_dir / f"run-{safe}.log"

    def run(self, run_id: str, version_info: dict):
        logger = logging.getLogger(f"dictsvc.run.{run_id}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        path = self.path_for(run_id)
        handler = logging.FileHandler(path, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        logger.handlers = [handler]
        logger.info("run_id=%s started_at=%s", run_id,
                    datetime.now(timezone.utc).isoformat())
        logger.info("versions %s", json.dumps(version_info, sort_keys=True))
        return _RunLog(logger, handler, path)


class _RunLog:
    def __init__(self, logger, handler, path: Path) -> None:
        self.logger = logger
        self.handler = handler
        self.path = path

    def event(self, payload: dict) -> None:
        self.logger.info("step %s", json.dumps(payload, default=str,
                                               sort_keys=True))

    def verdict(self, ok: bool, basis: str, extra: dict | None = None) -> None:
        self.logger.info("VERDICT ok=%s basis=%s extra=%s",
                         ok, basis, json.dumps(extra or {}, default=str,
                                               sort_keys=True))

    def close(self) -> None:
        self.handler.close()
        self.logger.removeHandler(self.handler)
