"""本地启动入口：python -m acstream"""

from __future__ import annotations

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.load()
    uvicorn.run(
        "acstream.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
