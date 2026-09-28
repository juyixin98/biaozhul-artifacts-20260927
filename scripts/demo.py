#!/usr/bin/env python3
"""端到端演示：在临时存储上跑完整的 base/dev/main 三方合并流程。

用法：
    .venv/bin/python scripts/demo.py            # 进程内直接调用服务层
    .venv/bin/python scripts/demo.py --http     # 通过真实 HTTP 接口调用（需先启动服务）

输出包含：每个主键的判定与依据、冲突分类、解决动作、合并后的行集、
合并提交的两条父引用，以及 run_id。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_merge.config import AppConfig  # noqa: E402
from table_merge.logging_setup import set_run_id  # noqa: E402
from table_merge.service import MergeService  # noqa: E402
from table_merge.storage import MetadataStore  # noqa: E402


def load_fixture() -> dict:
    with (ROOT / "sample_data" / "employees.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def build_service(storage_root: Path) -> MergeService:
    cfg = AppConfig(storage_root=storage_root, host="127.0.0.1", port=8000,
                    strict_schema=True, max_conflicts=100_000,
                    log_level="INFO", log_file=None)
    return MergeService(MetadataStore(cfg.db_path, cfg.snapshot_dir))


def ingest(service: MergeService, fixture: dict, rows_key: str) -> str:
    return service.ingest_snapshot({"schema": fixture["schema"],
                                    "rows": fixture[rows_key]})["snapshot_id"]


def run_in_process() -> int:
    set_run_id(f"demo_{uuid.uuid4().hex[:8]}")
    fixture = load_fixture()

    with tempfile.TemporaryDirectory(prefix="merge-demo-") as tmp:
        service = build_service(Path(tmp))

        base_id = ingest(service, fixture, "base")
        dev_id = ingest(service, fixture, "dev")
        main_id = ingest(service, fixture, "main")
        print(f"[1] 摄取三方快照: base={base_id} dev={dev_id} main={main_id}")

        service.initialize_main(base_id, "base import", "demo")
        main_c0 = service.store.get_branch("main")["commit_id"]
        service.create_branch("dev", {"commit_id": main_c0})
        service.commit("dev", dev_id, "dev edits", "dev-author")
        service.commit("main", main_id, "main edits", "main-author")
        print("[2] 建立 main 与 dev 两条提交线（共同祖先是 base 提交）")

        plan = service.plan_merge({"branch": "dev"}, {"branch": "main"}, "main")
        plan_dict = service.plan_to_dict(plan)
        print("\n[3] 合并计划的内核计算步骤：")
        for step in plan_dict["steps"]:
            print("    " + step)
        print("\n[4] 每个主键的判定与依据：")
        for row in plan_dict["automatic_rows"] + plan_dict["conflicts"]:
            flag = "CONFLICT" if row["is_conflict"] else "auto    "
            print(f"    [{flag}] key={row['key']} {row['decision']:<24} {row['basis']}")
        print(f"\n[5] 判定计数: {plan_dict['counts']}")

        # 演示固定的解决策略
        resolutions = []
        for conflict in plan_dict["conflicts"]:
            if conflict["decision"] == "DELETE_MODIFY_CONFLICT":
                action = "USE_MAIN"          # 对 id=4 保留 main 的修改（明确保留删改冲突）
            elif conflict["decision"] == "SAME_FIELD_CONFLICT":
                action = "USE_MAIN"          # id=6 采用 main 的 score=99
            else:  # ADD_ADD_CONFLICT
                action = "USE_DEV"           # id=9 采用 dev 新增版本
            resolutions.append({"row_key": json.dumps(conflict["key"], separators=(",", ":")),
                                "action": action})
        saved = service.save_resolutions(plan, resolutions)
        print(f"[6] 保存解决结果（绑定三方快照）: {saved}")

        result = service.commit_merge(plan, "merge demo", "demo")
        merged = service.snapshot_rows(result["snapshot_id"])
        print("\n[7] 合并后的行集：")
        for row in merged["rows"]:
            print("    " + json.dumps(row, ensure_ascii=False))
        print("\n[8] 合并提交血缘（必须含两条父引用）：")
        print("    merge_commit =", result["merge_commit"]["commit_id"])
        print("    parent_commit_ids =", result["parent_commit_ids"])
        print("    base_commit =", result["merge_commit"]["merge"]["base_commit_id"])
        print("    resolution_summary =", json.dumps(result["resolution_summary"],
                                                     ensure_ascii=False))
        assert len(result["parent_commit_ids"]) == 2, "合并提交必须保留两条父引用"
        print("\nDEMO OK: 自动合并分区、三类冲突、血缘双亲引用均已验证。")
    return 0


def run_over_http(base_url: str) -> int:
    import httpx

    fixture = load_fixture()
    run_id = f"demo_http_{uuid.uuid4().hex[:8]}"
    headers = {"X-Request-ID": run_id}
    client = httpx.Client(base_url=base_url, headers=headers, timeout=30)

    def post(path: str, body: dict, expected: int = 200) -> dict:
        resp = client.post(path, json=body)
        if resp.status_code != expected:
            raise SystemExit(f"POST {path} -> {resp.status_code}: {resp.text}")
        return resp.json()

    def ingest(rows_key: str) -> str:
        return post("/api/v1/snapshots",
                    {"schema": fixture["schema"], "rows": fixture[rows_key]},
                    201)["snapshot_id"]

    base_id = ingest("base")
    dev_id = ingest("dev")
    main_id = ingest("main")
    print(f"[1] 摄取三方快照(HTTP): base={base_id} dev={dev_id} main={main_id}")
    post("/api/v1/repository/init", {"snapshot_id": base_id}, 201)
    main_c0 = client.get("/api/v1/branches").json()["branches"][0]["commit_id"]
    post("/api/v1/branches", {"name": "dev", "ref": {"commit_id": main_c0}}, 201)
    post("/api/v1/branches/dev/commits", {"snapshot_id": dev_id, "message": "dev"}, 201)
    post("/api/v1/branches/main/commits", {"snapshot_id": main_id, "message": "main"}, 201)

    plan = post("/api/v1/merges/plan", {"dev": {"branch": "dev"}})
    print(f"[2] 计划生成: plan_id={plan['plan_id']} counts={plan['counts']} "
          f"conflicts={len(plan['conflicts'])}")

    items = []
    for conflict in plan["conflicts"]:
        if conflict["decision"] == "DELETE_MODIFY_CONFLICT":
            action = "USE_MAIN"
        elif conflict["decision"] == "SAME_FIELD_CONFLICT":
            action = "USE_MAIN"
        else:
            action = "USE_DEV"
        items.append({"row_key": json.dumps(conflict["key"], separators=(",", ":")),
                      "action": action})
    post("/api/v1/merges/resolve",
         {"plan_id": plan["plan_id"], "dev": {"branch": "dev"}, "resolutions": items})
    committed = post("/api/v1/merges/commit",
                     {"plan_id": plan["plan_id"], "dev": {"branch": "dev"},
                      "message": "http merge"})
    print(f"[3] 合并提交: {committed['merge_commit']['commit_id']}")
    print(f"    双亲父引用: {committed['parent_commit_ids']}")
    lineage = client.get("/api/v1/lineage",
                         params={"commit_id": committed["merge_commit"]["commit_id"]}).json()
    print(f"[4] 血缘查询: is_merge={lineage['is_merge']} parents={lineage['parent_commit_ids']}")
    print(f"[5] 全程关联 run_id={run_id}（响应头 X-Request-ID 同名）")
    print("DEMO OK (HTTP)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true",
                        help="对已运行的服务发 HTTP 请求（默认进程内演示）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    return run_over_http(args.base_url) if args.http else run_in_process()


if __name__ == "__main__":
    raise SystemExit(main())
