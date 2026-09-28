"""``python -m colaudit`` 启动审计服务。"""
from __future__ import annotations

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.load()
    uvicorn.run(
        "colaudit.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
