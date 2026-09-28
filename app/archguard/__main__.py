"""Local entry point: ``python -m archguard`` starts uvicorn."""

from __future__ import annotations

import argparse

import uvicorn

from .config import Config


def main() -> None:
    parser = argparse.ArgumentParser(description="archguard service")
    parser.add_argument("--config", help="path to a JSON config file")
    parser.add_argument("--host", help="bind host (overrides config)")
    parser.add_argument("--port", type=int, help="bind port (overrides config)")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    uvicorn.run(
        "archguard.api:create_app",
        factory=True,
        host=args.host or cfg.host,
        port=args.port or cfg.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
