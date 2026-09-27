"""Local ASGI entrypoint: ``python -m app.main`` or ``uvicorn app.main:app``."""

from __future__ import annotations

import uvicorn

from .api import app  # noqa: F401  (imported for uvicorn discovery)

__all__ = ["app"]


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
