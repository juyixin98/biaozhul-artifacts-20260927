"""Runnable service entrypoint: ``python -m app.main`` or ``uvicorn app.main:app``."""
from __future__ import annotations

from .api import app  # exposed as ``app.main:app`` for uvicorn
from .config import Settings
from .logging_setup import configure_logging

__all__ = ("app",)

configure_logging(Settings.from_env().log_level)

if __name__ == "__main__":
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level=settings.log_level.lower(),
    )
