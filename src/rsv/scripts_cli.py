"""rsv-replay 命令行：离线重放 bundle 文件并输出 JSON 报告。"""

from __future__ import annotations

import argparse
import json
import sys

from .replay import replay_bundle_file


def replay_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RSV offline bundle replay")
    p.add_argument("bundle", help="path to bundle JSON")
    p.add_argument("--runs-dir", default="runs/replay", help="directory for JSONL run logs")
    p.add_argument("--out", help="write report JSON to this path as well")
    args = p.parse_args(argv)

    res = replay_bundle_file(args.bundle, runs_dir=args.runs_dir)
    payload = json.dumps(res.to_dict(), indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
    print(payload)

    # 全接受才退出 0；存在被拒交易时退出 2（便于 CI 精确区分“工具坏了”与“预期拒绝”）
    return 0 if res.rejected_count == 0 else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(replay_main())
