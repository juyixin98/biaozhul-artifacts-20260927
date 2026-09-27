"""Structured-ish logging used by both the service and the core engine.

Log records carry a run/job identity in the first bracketed field so test logs
can be correlated with the inputs that produced them.
"""
from __future__ import annotations

import logging
import sys

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("subguard")
    if root.handlers:
        root.setLevel(level)
        return
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"subguard.{name}")
