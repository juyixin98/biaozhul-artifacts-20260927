#!/usr/bin/env python3
"""离线回放 CLI。

用法：
    python -m scripts.replay --fixture tests/fixtures/chain_fixture.json [--db data/chain.db]

期望不符时以非零退出码结束，并在 test-logs/ 写结构化日志。
"""

from __future__ import annotations

import argparse
import sys

from app.replay import ReplayError, replay


def main() -> int:
    ap = argparse.ArgumentParser(description="离线回放链状态夹具")
    ap.add_argument("--fixture", default="tests/fixtures/chain_fixture.json")
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--log-dir", default="test-logs")
    args = ap.parse_args()

    try:
        result = replay(args.fixture, args.db, log_dir=args.log_dir)
    except ReplayError as e:
        print(f"回放失败 [{e.code}]: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"夹具不存在: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # 未知异常绝不静默成成功
        print(f"回放内部错误: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("回放成功：", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
