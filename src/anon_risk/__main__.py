"""命令行入口：python -m anon_risk [--host 127.0.0.1] [--port 8080]。"""

from __future__ import annotations

import argparse

import uvicorn

from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="anon-risk 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--config", default=None, help="TOML 配置路径")
    args = parser.parse_args()

    settings = load_settings(args.config)
    print(f"anon-risk {settings.app.version} "
          f"(metric_version={settings.app.metric_version}) "
          f"config={settings.source_path}")
    uvicorn.run(
        "anon_risk.api.main:app",
        host=args.host, port=args.port, reload=args.reload,
    )


if __name__ == "__main__":
    main()
