"""FastAPI dependencies: request id, diagnostics and the version registry."""
from __future__ import annotations

from typing import Optional

from fastapi import Header, Request

from ..config import Settings
from ..diagnostics import RequestDiagnostics, new_request_id
from ..storage.registry import VersionRegistry


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_registry(request: Request) -> VersionRegistry:
    registry: Optional[VersionRegistry] = getattr(request.app.state, "registry", None)
    if registry is None:  # service not initialized (misconfiguration)
        raise RuntimeError("registry not initialized; application lifespan did not run")
    return registry


def get_diagnostics(
    request: Request,
    x_request_id: Optional[str] = Header(default=None),
) -> RequestDiagnostics:
    request_id = (x_request_id or new_request_id()).strip() or new_request_id()
    reveal = bool(getattr(request.app.state.settings, "log_reveal_text", False))
    diag = RequestDiagnostics(request_id=request_id, reveal_text=reveal)
    request.state.diag = diag
    return diag


def get_pinned_version(x_dictionary_version: Optional[str] = Header(default=None)) -> Optional[str]:
    ref = x_dictionary_version
    if ref is not None:
        ref = ref.strip() or None
    return ref
