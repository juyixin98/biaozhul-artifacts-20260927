#!/usr/bin/env python3
"""重放测试运行日志。

用法：
    python scripts/replay.py                     # 重放最近一次运行（test-logs/latest）
    python scripts/replay.py testrun-2026...     # 重放指定运行编号
    python scripts/replay.py --case <子串>        # 只看重放标题含某子串的用例
    python scripts/replay.py --failed             # 只看重放失败/出错用例
    python scripts/replay.py --run-command        # 打印用于复现的 pytest 命令

每条用例日志含 seq 编号、关键中间状态与判断理由；本脚本按时间顺序打印，
并在结尾汇总通过/失败与失败类别，便于定位“计划与参考不一致”类问题。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_ROOT = ROOT / "test-logs"


def resolve_run(run_id: str | None) -> Path:
    if run_id:
        p = LOG_ROOT / run_id
        if not p.exists():
            raise SystemExit(f"找不到运行目录: {p}")
        return p
    latest = LOG_ROOT / "latest"
    if not latest.exists():
        raise SystemExit("没有 test-logs/latest；先运行 pytest。")
    return LOG_ROOT / latest.read_text(encoding="utf-8").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--case", help="只看 nodeid 含该子串的用例")
    ap.add_argument("--failed", action="store_true", help="只看失败/出错用例")
    ap.add_argument("--run-command", action="store_true", help="打印复现命令后退出")
    args = ap.parse_args()

    run_dir = resolve_run(args.run_id)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))

    if args.run_command:
        print(f"NRP_TEST_LOG_DIR=test-logs {sys.executable} -m pytest tests/ -v")
        return 0

    print(f"== 运行编号 {meta['run_id']} ==")
    print(f"   python {meta['python'].split()[0]}  平台 {meta['machine']}")
    print(f"   依赖 {meta['package_versions']}")
    print(f"   目录 {run_dir}\n")

    cases = sorted((run_dir / "cases").glob("*.jsonl"))
    n_shown = 0
    for cf in cases:
        lines = [json.loads(l) for l in cf.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            continue
        nodeid = lines[0].get("message", cf.stem)
        status = next(
            (x.get("data", {}).get("status")
             for x in reversed(lines) if x["event"] == "case_end"),
            "?",
        )
        if args.failed and status not in ("failed", "error"):
            continue
        if args.case and args.case not in nodeid:
            continue
        n_shown += 1
        marker = {"passed": "✓", "failed": "✗", "error": "!", "skipped": "○"}.get(status, "?")
        print(f"{marker} [{status}] {nodeid}")
        for rec in lines:
            ev = rec["event"]
            if ev == "case_start":
                continue
            if ev == "intermediate_state":
                val = rec.get("data", {}).get("value")
                shown = json.dumps(val, ensure_ascii=False)
                if len(shown) > 400:
                    shown = shown[:400] + f" …(+{len(shown)-400} chars)"
                print(f"    state  {rec['message']}: {shown}")
            elif ev == "assertion":
                d = rec["data"]
                print(f"    assert {'OK ' if d.get('ok') else 'BAD'} {rec['message']}")
                if not d.get("ok"):
                    print(f"           expected={json.dumps(d.get('expected'), ensure_ascii=False)}")
                    print(f"           actual  ={json.dumps(d.get('actual'), ensure_ascii=False)}")
            elif ev == "failure_category":
                d = rec["data"]
                print(f"    expect-fail {d['code']} ({d['category']}, HTTP {d['http_status']})")
            elif ev == "case_end":
                if status in ("failed", "error"):
                    print(f"    -> {status}: {rec.get('data', {}).get('detail', '')}")
        print()

    summary_file = run_dir / "summary.json"
    if summary_file.exists():
        s = json.loads(summary_file.read_text(encoding="utf-8"))
        print(f"汇总: {s['counts']}  共 {s['total']} 用例")
        if s["failed_cases"]:
            print("失败/出错:")
            for f in s["failed_cases"]:
                print(f"  - {f['case']}: {f['detail'][:160]}")
    print(f"\n显示 {n_shown} 个用例。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
