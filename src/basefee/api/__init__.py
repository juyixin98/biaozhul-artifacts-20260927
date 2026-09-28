"""HTTP/API layer: FastAPI app, schemas, wire codecs and structured logging."""

from .app import create_app, app

__all__ = ["create_app", "app"]
