"""Local service launcher: ``python -m reorgindex.api.serve``."""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "reorgindex.api.main:create_app",
        factory=True,
        host=os.environ.get("REORG_HOST", "127.0.0.1"),
        port=int(os.environ.get("REORG_PORT", "8080")),
        log_level=os.environ.get("REORG_UVICORN_LEVEL", "warning"),
    )


if __name__ == "__main__":
    main()
