"""支持 ``python -m app.offline`` 运行离线回放 CLI。"""
import sys

from .replay_cli import main

if __name__ == "__main__":
    sys.exit(main())
