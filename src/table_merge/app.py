"""本地服务入口：python -m table_merge.app 或 uvicorn table_merge.api:app。"""
from __future__ import annotations

import uvicorn

from .config import load_config
from .logging_setup import setup_logging


def main() -> None:
    cfg = load_config("config/dev.yaml")
    setup_logging(cfg.log_level, cfg.log_file)
    uvicorn.run(
        "table_merge.api:app",
        app_dir="src",
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
