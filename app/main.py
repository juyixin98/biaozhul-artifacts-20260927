"""可运行的服务入口。

启动方式::

    python -m app.main                 # 读取环境变量/默认配置
    CTRIE_DB_PATH=/tmp/demo.db python -m app.main --host 127.0.0.1 --port 8000
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Unicode 压缩 Trie 补全后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = create_app(settings)
    uvicorn.run(app, host=args.host, port=args.port,
                log_level=settings.log_level.lower())


app = create_app()


if __name__ == "__main__":
    main()
