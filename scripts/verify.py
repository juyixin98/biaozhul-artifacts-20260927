#!/usr/bin/env python3
"""端到端验证脚本。

执行两类检查并打印结果:
1. 单元/集成测试(pytest)。
2. 端到端:通过 FastAPI 接口跑全部夹具,核对状态、文本、冲突范围与显式重建。

无法在本环境执行的检查列在 UNEXECUTED_CHECKS,如实报告,不计入通过。
退出码:全部已执行检查通过为 0,否则为 1。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures"

UNEXECUTED_CHECKS = [
    "真实 uvicorn 网络部署检查(本验证使用进程内 TestClient,未绑定端口)",
    "并发写入压力测试(SQLite 单写者语义未在负载下验证)",
    "大文件性能基准(未设定吞吐/时延指标)",
]


def run_pytest(python: str) -> bool:
    print("== 1/2 单元与集成测试 (pytest) ==")
    proc = subprocess.run(
        [python, "-m", "pytest"], cwd=ROOT, capture_output=True, text=True
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print(proc.stderr.strip())
    print(f"pytest: {'PASS' if proc.returncode == 0 else 'FAIL'}\n")
    return proc.returncode == 0


def run_e2e() -> bool:
    print("== 2/2 端到端 API 检查(全部夹具)==")
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.config import Settings

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(
            Settings(db_path=str(Path(tmp) / "e2e.sqlite3"),
                     log_path=str(Path(tmp) / "e2e.log"))
        )
        client = TestClient(app)
        for path in sorted(FIXTURES_DIR.glob("*.json")):
            fx = json.loads(path.read_text(encoding="utf-8"))
            expected = fx["expected"]
            resp = client.post(
                "/merge",
                json={"base": fx["base"], "local": fx["local"],
                      "remote": fx["remote"], "document_id": fx["name"]},
            )
            body = resp.json()
            problems = []
            if resp.status_code != 200:
                problems.append(f"HTTP {resp.status_code}")
            else:
                if body["status"] != expected["status"]:
                    problems.append(
                        f"status {body['status']!r} != {expected['status']!r}")
                if body["text"] != expected["text"]:
                    problems.append("merged text mismatch")
                got_kinds = [c["kind"] for c in body["conflicts"]]
                want_kinds = [c["kind"] for c in expected["conflicts"]]
                if got_kinds != want_kinds:
                    problems.append(f"conflict kinds {got_kinds} != {want_kinds}")
                for c, want in zip(body["conflicts"], expected["conflicts"]):
                    for key in ("base_range", "local_range", "remote_range"):
                        if c[key] != want[key]:
                            problems.append(f"{key} {c[key]} != {want[key]}")
                for side, want_text in fx["resolutions"].items():
                    choices = {str(i): side
                               for i in range(len(body["conflicts"]))}
                    r = client.post(f"/merges/{body['merge_id']}/resolve",
                                    json={"choices": choices})
                    if r.status_code != 200 or \
                            r.json()["resolved_text"] != want_text:
                        problems.append(f"resolution {side!r} mismatch")
            status = "PASS" if not problems else "FAIL"
            ok = ok and not problems
            print(f"  [{status}] {fx['name']}" +
                  ("" if not problems else ": " + "; ".join(problems)))
    print(f"e2e: {'PASS' if ok else 'FAIL'}\n")
    return ok


def main() -> int:
    python = sys.executable
    results = {"pytest": run_pytest(python), "e2e": run_e2e()}
    print("== 未执行的检查(如实列出,不计入通过)==")
    for item in UNEXECUTED_CHECKS:
        print(f"  [SKIP] {item}")
    print()
    if all(results.values()):
        print("全部已执行检查通过。")
        return 0
    failed = [k for k, v in results.items() if not v]
    print(f"存在失败检查: {', '.join(failed)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
