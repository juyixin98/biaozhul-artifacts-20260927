#!/usr/bin/env python3
"""端到端验证脚本（可独立于 pytest 运行）：

1. 在临时仓库启动应用（ASGI 进程内），重放全部合成夹具场景；
2. 三方对照：手写逐版本期望 × 独立 oracle × HTTP 服务；
3. 额外断言逐版本事件顺序、删除理由与运行日志可重放性；
4. 输出 reports/verify-report-<编号>.json（run_id、每版本每文件每行的处置依据、失败类别）。

退出码：0 全部通过；1 存在失败（失败按类别归组，不吞错）。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.api.app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from fixtures import load_all  # noqa: E402
from fixtures.replay import ReplayFailure, replay_scenario  # noqa: E402


def _disposition_evidence(client, table_id: str, snapshot_id: str) -> dict:
    """拉取一次 explain，整理成“每行被保留或删除的依据”结构。"""
    r = client.post(f"/tables/{table_id}/explain", json={"snapshot_id": snapshot_id})
    r.raise_for_status()
    body = r.json()
    rows = []
    for row in body["rows"]:
        rows.append({
            "file_id": row["file_id"],
            "position": row["position"],
            "added_seq": row["added_seq"],
            "disposition": row["disposition"],
            "row": row["row"],
            "basis": [
                {
                    "rule": reason["kind"],
                    "delete_file_id": reason["delete_file_id"],
                    "delete_seq": reason["seq"],
                    "matched_key": reason.get("key"),
                }
                for reason in row["reasons"]
            ] if row["reasons"] else
                (["survived all position & equality deletes"]
                 if row["disposition"] == "KEPT" else
                 ["excluded by post-delete filter"]),
        })
    return {
        "run_id": body["run_id"],
        "snapshot_id": body["snapshot_id"],
        "seq": body["seq"],
        "intermediate_state": body["intermediate_state"],
        "null_keys_ignored": body["null_keys_ignored"],
        "delete_files_visible": body["delete_files"],
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RTDA 端到端验证")
    parser.add_argument("--warehouse", help="仓库目录（默认使用临时目录，验证后保留需显式指定）")
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    run_no = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"verify-report-{run_no}.json"

    report: dict = {
        "run_no": run_no,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": [],
        "summary": {"passed": 0, "failed": 0, "failures_by_category": {}},
    }

    warehouse = args.warehouse
    temp_dir = None
    if warehouse is None:
        temp_dir = tempfile.mkdtemp(prefix="rtda-verify-")
        warehouse = temp_dir

    client = TestClient(create_app(Config.from_env(warehouse)))
    try:
        for scenario in load_all():
            entry = {"name": scenario.name, "description": scenario.description, "status": "PASS",
                     "versions": [], "error": None}
            try:
                result = replay_scenario(client, scenario)
                # 对每个成功版本留存证据快照
                for v in result.versions:
                    if v.status != "OK" or v.snapshot_id is None:
                        entry["versions"].append({
                            "seq": None, "status": "EXPECTED_ERROR", "run_ids": v.run_ids,
                        })
                        continue
                    evidence = _disposition_evidence(client, result.table_id, v.snapshot_id)
                    events = client.get(
                        f"/tables/{result.table_id}/events", params={"seq": v.seq}
                    ).json()["events"]
                    entry["versions"].append({
                        "seq": v.seq,
                        "snapshot_id": v.snapshot_id,
                        "status": "OK",
                        "run_ids": v.run_ids,
                        "evidence": evidence,
                        "events_through_version": [
                            {"seq": e["seq"], "type": e["event_type"], "payload": e["payload"]}
                            for e in events if e["event_type"] != "TABLE_CREATED"
                        ],
                    })
                entry["table_id"] = result.table_id
                report["summary"]["passed"] += 1
            except ReplayFailure as exc:
                entry["status"] = "FAIL"
                entry["error"] = {"category": exc.category, "message": str(exc), "detail": exc.detail}
                report["summary"]["failed"] += 1
                report["summary"]["failures_by_category"][exc.category] = \
                    report["summary"]["failures_by_category"].get(exc.category, 0) + 1
            except Exception as exc:  # 任何未预期异常也归类，不吞错
                entry["status"] = "FAIL"
                entry["error"] = {"category": "UNEXPECTED_EXCEPTION", "message": str(exc),
                                  "traceback": traceback.format_exc()}
                report["summary"]["failed"] += 1
                report["summary"]["failures_by_category"]["UNEXPECTED_EXCEPTION"] = \
                    report["summary"]["failures_by_category"].get("UNEXPECTED_EXCEPTION", 0) + 1
            report["scenarios"].append(entry)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["warehouse"] = warehouse
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str),
                               encoding="utf-8")

    for s in report["scenarios"]:
        flag = "PASS" if s["status"] == "PASS" else f"FAIL {s['error']['category']}"
        print(f"[{flag}] {s['name']} — {s['description']}")
    print(f"\n报告: {report_path}")
    print(f"通过 {report['summary']['passed']} / 失败 {report['summary']['failed']}")
    if temp_dir:
        print(f"临时仓库（含 Parquet/SQLite/运行日志）: {temp_dir}")
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
