"""Structured, run-correlated logging.

Every log line carries:

* the service and protocol version,
* a ``run_id`` (per-request when supplied via ``X-Run-Id``, else generated),
* an optional input correlation fingerprint,
* the computation step and, for verifications, the judgement basis.

Handlers are attached per configured log directory but named loggers are
idempotent, so test setups can re-point them at a temp directory.
"""
from __future__ import annotations

import logging
import os
import platform
import secrets
import sys
from logging import Logger
from pathlib import Path

from app import SERVICE_VERSION
from app.config import PROTOCOL_VERSION, Settings

LOGGER_NAME = "audit_commitment"
_RUN_LOGGER_MARK = "_run_token"


def new_run_id() -> str:
    return "run-" + secrets.token_hex(6)


class _RunFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.run_id = "-"
        self.input_fp = "-"

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = getattr(record, "run_id", None) or self.run_id
        record.input_fp = getattr(record, "input_fp", None) or self.input_fp
        record.service_version = SERVICE_VERSION
        record.protocol_version = PROTOCOL_VERSION
        return True


class RunBoundLogger:
    """Convenience wrapper that stamps ``run_id``/``input_fp`` on every line."""

    def __init__(self, logger: Logger, run_filter: _RunFilter, run_id: str):
        self._logger = logger
        self._filter = run_filter
        self.run_id = run_id

    def bind_input(self, fingerprint: str) -> None:
        self._filter.input_fp = fingerprint

    def step(self, message: str, **extra: object) -> None:
        self._logger.info(message, extra={"run_id": self.run_id, **extra})

    def warn(self, message: str, **extra: object) -> None:
        self._logger.warning(message, extra={"run_id": self.run_id, **extra})

    def error(self, message: str, **extra: object) -> None:
        self._logger.error(message, extra={"run_id": self.run_id, **extra})

    def verdict(self, valid: bool, category: str | None, reason: str, **extra: object) -> None:
        level = logging.INFO if valid else logging.WARNING
        self._logger.log(
            level,
            "verdict %s category=%s reason=%s",
            "ACCEPT" if valid else "REJECT",
            category or "-",
            reason,
            extra={"run_id": self.run_id, **extra},
        )


def configure_logging(settings: Settings) -> Logger:
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, settings.log_level))
    logger.propagate = False
    # Replace handlers so tests / restarts do not duplicate lines.
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    fmt = logging.Formatter(
        "%(asctime)sZ %(levelname)s svc=%(service_version)s "
        "proto=%(protocol_version)s run=%(run_id)s input=%(input_fp)s "
        "%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    run_filter = _RunFilter()
    fh = logging.FileHandler(Path(settings.log_dir) / "service.log", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(run_filter)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.addFilter(run_filter)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.info(
        "logging configured python=%s platform=%s pid=%s log_dir=%s level=%s",
        platform.python_version(),
        platform.platform(),
        os.getpid(),
        settings.log_dir,
        settings.log_level,
    )
    setattr(logger, _RUN_LOGGER_MARK, run_filter)
    return logger


def bind_run(logger: Logger, run_id: str | None = None) -> RunBoundLogger:
    run_filter = getattr(logger, _RUN_LOGGER_MARK, None)
    if run_filter is None:  # pragma: no cover - configure_logging always runs
        run_filter = _RunFilter()
        logger.addFilter(run_filter)
    rid = run_id or new_run_id()
    run_filter.run_id = rid
    run_filter.input_fp = "-"
    return RunBoundLogger(logger, run_filter, rid)
