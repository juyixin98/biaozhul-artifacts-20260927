"""uvicorn ``--factory`` 入口：``local-txpool serve`` 与外部 ASGI 启动共用。"""

from __future__ import annotations

import os

from .api.app import create_app
from .config import Config
from .logging_setup import configure_logging
from .service import Service


def app_factory():
    cfg_path = os.environ.get("LOCALTXPOOL_CONFIG")
    config = Config.load(cfg_path) if cfg_path else Config()
    configure_logging(config.log.level, config.log.json)
    service = Service(config)
    return create_app(service)
