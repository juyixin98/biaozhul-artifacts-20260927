"""ASGI entry point: ``uvicorn app.main:app``."""
from __future__ import annotations

import os

from .api.routes import create_app
from .api.service import VerificationService
from .config import settings
from .logging_config import configure_logging
from .metadata.store import MetadataStore

configure_logging(os.environ.get("PV_LOG_LEVEL", "INFO"))
settings.ensure_dirs()

_store = MetadataStore(settings.db_path)
service = VerificationService(store=_store)
app = create_app(service)
