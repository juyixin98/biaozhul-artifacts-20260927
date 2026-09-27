"""Logging helpers: every pipeline/API log line carries request and job identity."""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger("driftcorr")
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"driftcorr.{name}")


def log_step(logger: logging.Logger, *, request_id: str, job_id: str,
             step: str, message: str, **fields: object) -> None:
    """Emit one structured, greppable line per pipeline step."""
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info(
        "request_id=%s job_id=%s step=%s %s%s",
        request_id, job_id, step, message, f" {extras}" if extras else "",
    )
