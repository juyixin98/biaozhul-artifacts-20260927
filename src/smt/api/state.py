"""Shared FastAPI application state."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings


@dataclass
class AppState:
    settings: Settings
    service: "object"  # smt.services.StateService
    logger: logging.Logger
