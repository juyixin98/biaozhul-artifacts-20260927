"""跨进程重放核验。

做法（两进程隔离，参考答案不由被测进程自身生成）：

1. 进程 A（builder）：用固定合成夹具构建一条链（成功/临界 gas/REVERT/异常/
   嵌套调用），把交易信封写入 SQLite，并导出收据清单到 JSON；
2. 进程 B（replay）：只信任信封，新建干净内核重放，逐字段比对并输出报告；
3. 测试断言：进程 B 报告 ok、tx 数一致、最终状态根等于**测试里硬编码**的
   期望值（该值由我们在构建后固化，任何语义漂移都会让断言失败）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

BUILDER = os.path.join(os.path.dirname(__file__), "helpers", "build_chain.py")


def _run(script: str, db: str, out: str) -> subprocess.CompletedProcess:
    repo = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ, PYTHONPATH=os.path.join(repo, "src"))
    return subprocess.run(
        [sys.executable, script, "--db", db, "--out", out],
        capture_output=True, text=True, env=env, check=False,
    )


def test_two_processes_replay_identical_receipts(tmp_path):
    db = str(tmp_path / "chain.db")
    out = str(tmp_path / "receipts.json")

    # 进程 A：构建
    p1 = _run(BUILDER, db, out)
    assert p1.returncode == 0, f"builder failed:\n{p1.stderr}\n{p1.stdout}"
    built = json.loads(p1.stdout)
    with open(out) as fh:
        receipts = json.load(fh)

    # 进程 B：离线回放（独立解释器、独立内核实例）
    repo = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ, PYTHONPATH=os.path.join(repo, "src"))
    p2 = subprocess.run(
        [sys.executable, "-m", "teachchain.replay", "--db", db, "--all"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert p2.returncode == 0, f"replay failed:\n{p2.stderr}\n{p2.stdout}"
    report = json.loads(p2.stdout)
    assert report["ok"] is True
    assert report["tx_total"] == built["tx_total"] == len(receipts)
    assert report["matched"] == report["tx_total"]
    assert report["mismatches"] == []

    # 硬编码期望值：任何语义/计费变化都会破坏这些常量
    assert built["tx_total"] == 9
    # 失败类别逐笔锚定
    by_height = {r["height"]: r for r in receipts}
    assert by_height[3]["halt_code"] == "out_of_gas"
    assert by_height[3]["status"] == 0
    assert by_height[5]["halt_code"] == "invalid_instruction"
    assert by_height[7]["halt_code"] == "revert"
    assert by_height[7]["reverted"] is True
    assert by_height[9]["status"] == 1  # 父调用在子失败后仍成功

    # 最终状态根：跨进程、跨测试重跑必须稳定（硬编码）
    assert report["final_state_root"] == built["final_state_root"]
    assert (
        report["final_state_root"]
        == "e56cfbcb265a5dabeb5ab11e975636f391403ca7495dde7159d96bc3e2f0fcf4"
    )


def test_replay_detects_tampered_receipt(tmp_path):
    """若有人篡改了已存收据，跨进程回放必须报不一致。"""
    import sqlite3
    db = str(tmp_path / "chain.db")
    out = str(tmp_path / "receipts.json")
    p1 = _run(BUILDER, db, out)
    assert p1.returncode == 0, p1.stderr

    # 直接篡改收据里的 gas_charged
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT receipt_json FROM receipts WHERE height=2"
    ).fetchone()
    import json as _json
    rec = _json.loads(row[0])
    rec["gas_charged"] += 1
    con.execute(
        "UPDATE receipts SET receipt_json=?, gas_charged=? WHERE height=2",
        (_json.dumps(rec, sort_keys=True), rec["gas_charged"]),
    )
    con.commit()
    con.close()

    repo = os.path.dirname(os.path.dirname(__file__))
    env = dict(os.environ, PYTHONPATH=os.path.join(repo, "src"))
    p2 = subprocess.run(
        [sys.executable, "-m", "teachchain.replay", "--db", db],
        capture_output=True, text=True, env=env, check=False,
    )
    # stop_on_first=True 时不一致 -> 非零退出
    assert p2.returncode != 0
    assert "replay mismatch" in p2.stderr.lower() or "replay mismatch" in p2.stdout.lower()
