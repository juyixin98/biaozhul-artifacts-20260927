"""命令行入口：deleter serve / deleter demo。"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deleter", description="湖表读时删除应用器")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="启动 FastAPI 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workspace", default=os.environ.get("DELETER_WORKSPACE", ".deleter_workspace"))
    demo_p = sub.add_parser("demo", help="运行内置演示场景并输出逐行依据")
    demo_p.add_argument("--workspace", default="demo_out")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        os.environ["DELETER_WORKSPACE"] = args.workspace
        uvicorn.run("deleter.api.app:create_app", factory=True,
                    host=args.host, port=args.port)
        return 0
    if args.cmd == "demo":
        from .demo import run_demo
        path = run_demo(args.workspace)
        print(f"演示完成，逐行依据与 run 日志在: {path}")
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
