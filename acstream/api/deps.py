"""路由公共依赖。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from fastapi import Request

from ..config import Settings
from ..diagnostics import Diagnostics
from ..services.matcher import MatcherService
from ..services.version_registry import VersionRegistry
from ..storage.db import DiagnosticStore


@dataclass
class AppState:
    settings: Settings
    registry: VersionRegistry
    service: MatcherService
    diagnostics: Diagnostics
    diagnostics_store: DiagnosticStore
    lock: threading.RLock


def get_state(request: Request) -> AppState:
    return request.app.state.deps
