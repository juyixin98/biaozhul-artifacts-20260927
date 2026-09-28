"""Service entry point: ``python -m lightclient.run``.

Reads LC_DB_PATH / LC_CHECKPOINT_KEY_HEX / LC_CHAIN_ID etc. from the
environment (see config.py) and serves the HTTP API with uvicorn.
"""

from __future__ import annotations

import uvicorn

from .service import create_app_from_env


def main() -> None:
    app = create_app_from_env()
    uvicorn.run(app, host="127.0.0.1", port=8088, log_level="info")


if __name__ == "__main__":
    main()
