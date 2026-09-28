"""Local development entrypoint: ``python run.py`` (or ``uvicorn app.api.app:app``)."""
from __future__ import annotations

import uvicorn

from app.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "app.api.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
