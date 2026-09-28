"""本地开发入口：python -m mediaconcat.main 或 uvicorn mediaconcat.api:app。"""
from __future__ import annotations

import uvicorn

from .config import settings

if __name__ == "__main__":
    uvicorn.run(
        "mediaconcat.api:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
