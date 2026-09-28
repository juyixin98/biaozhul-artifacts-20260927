"""Local runnable entrypoint: ``python -m app.main`` or ``uvicorn app.main:app``."""
from __future__ import annotations

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "app.api:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


# Exposed for ``uvicorn app.main:app``.
from .api import app  # noqa: E402,F401

if __name__ == "__main__":
    main()
