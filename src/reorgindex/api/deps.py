"""Shared construction for API and tests."""
from __future__ import annotations

from ..app import Application
from ..config import Settings


def build_app_state(settings: Settings, authorized_producers: set[str]) -> Application:
    return Application(settings, authorized_producers)
