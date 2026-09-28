"""Application factory: wires config -> storage -> service -> FastAPI."""

from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .api import router
from .config import AppConfig, load_config
from .logging_setup import configure_logging
from .replay import build_service
from .storage import Storage


def create_app(config: AppConfig | str | None = None) -> FastAPI:
    if config is None or isinstance(config, str):
        config = load_config(config or "configs/demo.json")
    logger, run_id, _ = configure_logging(config.log_dir)
    logger.info("starting ffg_slash detector",
                extra={"run_id": run_id, "version": __version__,
                       "chain_id": config.chain_id, "step": "startup",
                       "status": "ok"})

    storage = Storage(config.database)
    service = build_service(config, storage, run_id, logger=logger)

    app = FastAPI(title="FFG slashing evidence detector", version=__version__)
    app.state.config = config
    app.state.storage = storage
    app.state.service = service
    app.include_router(router)
    return app
