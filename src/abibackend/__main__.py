"""Entry point: ``python -m abibackend`` serves the API."""
from __future__ import annotations

import uvicorn

from .config import SETTINGS
from .log_utils import configure_logging


def main() -> None:
    configure_logging(SETTINGS.log_level)
    uvicorn.run(
        "abibackend.api:app",
        host=SETTINGS.api_host,
        port=SETTINGS.api_port,
        log_level=SETTINGS.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
