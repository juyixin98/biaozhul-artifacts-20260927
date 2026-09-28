"""uvicorn ASGI 入口：python -m uvicorn app.main:app"""

from .api import app

__all__ = ["app"]
