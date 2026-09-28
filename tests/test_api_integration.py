"""端到端 HTTP 集成测试：真实 ASGI 客户端 + 临时存储。

覆盖题目四类用例：相同修改 / 互斥新增 / 同键不同字段 / 删改冲突；
并断言失败类别、run_id 关联、过期计划、未解决冲突禁止提交等行为。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from table_merge.api import create_app
from table_merge.config import AppConfig

from .conftest import EMP_SCHEMA, row


@pytest.fixture
def client(tmp_path) -> TestClient:
    cfg = AppConfig(storage_root=tmp_path / "store", host="127.0.0.1", port=0,
                    strict_schema=True, max_conflicts=100_000,
                    log_level="ERROR", log_file=None)
    return TestClient(create_app(cfg))


def _ingest(client: TestClient, rows, request_id: str) -> str:
    resp = client.post("/api/v1/snapshots",
                       json={"schema": EMP_SCHEMA, "rows": rows},
                       headers={"X-Request-ID": request_id})
    assert resp.status_code == 201, resp.text
    assert resp.headers["X-Request-ID"] == request_id
    return resp.json()["snapshot_id"]


def _seed_history(client: TestClient, base, dev, main, run_id: str):
    base_id = _ingest(client, base, f"{run_id}-base")
    dev_id = _ingest(client, dev, f"{run_id}-dev")
    main_id = _ingest(client, main, f"{run_id}-main")

    r = client.post("/api/v1/repository/init",
                    json={"snapshot_id": base_id, "message": "base", "author": "t"},
                    headers={"X-Request-ID": run_id})
    assert r.status_code == 201, r.text
    main_c0 = r.json()["commit_id"]

    r = client.post("/api/v1/branches",
                    json={"name": "dev", "ref": {"commit_id": main_c0}},
                    headers={"X-Request-ID": run_id})
    assert r.status_code == 201, r.text
    r = client.post("/api/v1/branches/dev/commits",
                    json={"snapshot_id": dev_id, "message": "dev work"},
                    headers={"X-Request-ID": run_id})
    assert r.status_code == 201, r.text
    r = client.post("/api/v1/branches/main/commits",
                    json={"snapshot_id": main_id, "message": "main work"},
                    headers={"X-Request-ID": run_id})
    assert r.status_code == 201, r.text
    return base_id, dev_id, main_id


def test_full_merge_workflow_all_four_scenario_classes(client: TestClient):
    run_id = f"it_{uuid.uuid4().hex[:8]}"
    base = [
        row(1, "Alice", "bj", 10),   # 双方都不动
        row(2, "Bob", "sh", 20),     # 不相交字段
        row(4, "Dan", "hz", 40),     # dev 删 / main 改
        row(6, "Frank", "wh", 60),   # 同字段冲突
    ]
    dev = [
        row(1, "Alice", "bj", 10),
        row(2, "Bob", "bj", 25),                 # 改 city/score
        row(6, "Frank", "wh", 66),               # dev: score 66
        row(7, "Grace", "sz", 70),               # 仅 dev 新增
    ]
    main = [
        row(1, "Alice", "bj", 10),
        row(2, "Bob", "sh", 20, active=False),   # main: 改 active
        row(4, "Dan", "hf", 41),                 # main 修改了 dev 删掉的行
        row(6, "Frank", "wh", 99),               # main: score 99
        row(8, "Heidi", "tj", 80),               # 仅 main 新增
    ]
    _seed_history(client, base, dev, main, run_id)

    plan = client.post("/api/v1/merges/plan", json={"dev": {"branch": "dev"}},
                       headers={"X-Request-ID": run_id})
    assert plan.status_code == 200, plan.text
    assert plan.headers["X-Request-ID"] == run_id
    body = plan.json()
    assert body["has_conflicts"] is True
    counts = body["counts"]
    assert counts["UNCHANGED"] == 1
    assert counts["FAST_FORWARD"] == 2          # id=7、id=8 两条独占新增
    assert counts["FIELD_MERGE"] == 1           # id=2
    assert counts["SAME_FIELD_CONFLICT"] == 1   # id=6
    assert counts["DELETE_MODIFY_CONFLICT"] == 1  # id=4
    conflict_keys = {tuple(c["key"])[0]: c for c in body["conflicts"]}
    assert set(conflict_keys) == {4, 6}
    # 自动合并行中必须出现 id=2 的字段级合并结果
    auto2 = next(r for r in body["automatic_rows"] if r["key"] == [2])
    assert auto2["decision"] == "FIELD_MERGE"
    assert body["merged_row_count"] == 4        # 1 unchanged + 1 field-merge + 2 inserts
    # 计算步骤可观测
    assert any("step 3/4" in s for s in body["steps"])

    # 未解决冲突直接提交 -> 409 CONFLICT_STATE，而不是成功
    blocked = client.post("/api/v1/merges/commit",
                          json={"plan_id": body["plan_id"], "dev": {"branch": "dev"}},
                          headers={"X-Request-ID": run_id})
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "CONFLICT_STATE"
    assert "[4]" in blocked.json()["details"]["unresolved"]

    # 非法动作 -> 422 INVALID_RESOLUTION（明确失败类别）
    bad = client.post("/api/v1/merges/resolve", json={
        "plan_id": body["plan_id"], "dev": {"branch": "dev"},
        "resolutions": [{"row_key": "[4]", "action": "FIELD_PICK",
                         "field_picks": {"score": "DEV"}}],
    }, headers={"X-Request-ID": run_id})
    assert bad.status_code == 422
    assert bad.json()["error_code"] == "INVALID_RESOLUTION"

    # 对 id=4 用 KEEP_DELETED（接受 dev 的删除），id=6 用 USE_MAIN
    resolved = client.post("/api/v1/merges/resolve", json={
        "plan_id": body["plan_id"], "dev": {"branch": "dev"},
        "resolutions": [
            {"row_key": "[4]", "action": "KEEP_DELETED"},
            {"row_key": "[6]", "action": "USE_MAIN"},
        ],
    }, headers={"X-Request-ID": run_id})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["saved"] == 2

    committed = client.post("/api/v1/merges/commit", json={
        "plan_id": body["plan_id"], "dev": {"branch": "dev"},
        "message": "merge it", "author": "integrator",
    }, headers={"X-Request-ID": run_id})
    assert committed.status_code == 200, committed.text
    cbody = committed.json()
    assert len(cbody["parent_commit_ids"]) == 2  # 两条父引用
    assert cbody["row_count"] == 5              # 1,2,6,7,8（id=4 被删除）

    # 校验最终行集的具体字段值
    snap = client.get(f"/api/v1/snapshots/{cbody['snapshot_id']}",
                      headers={"X-Request-ID": run_id}).json()
    rows_by_id = {r["id"]: r for r in snap["rows"]}
    assert set(rows_by_id) == {1, 2, 6, 7, 8}
    assert rows_by_id[2] == row(2, "Bob", "bj", 25, active=False)  # 字段级合并
    assert rows_by_id[6]["score"] == 99.0                          # USE_MAIN
    assert 4 not in rows_by_id                                     # KEEP_DELETED

    # 血缘：合并提交是 merge、带双亲与共同祖先
    lineage = client.get("/api/v1/lineage",
                         params={"commit_id": cbody["merge_commit"]["commit_id"]},
                         headers={"X-Request-ID": run_id})
    assert lineage.status_code == 200
    lbody = lineage.json()
    assert lbody["is_merge"] is True
    assert lbody["parent_commit_ids"] == cbody["parent_commit_ids"]
    assert lbody["merge_detail"]["resolution_summary"]["final_row_count"] == 5
    assert lbody["merge_detail"]["resolution_summary"]["resolution_actions"] == {
        "KEEP_DELETED": 1, "USE_MAIN": 1,
    }

    # main 分支头已指向合并提交
    branches = client.get("/api/v1/branches", headers={"X-Request-ID": run_id}).json()
    main_head = next(b for b in branches["branches"] if b["name"] == "main")
    assert main_head["commit_id"] == cbody["merge_commit"]["commit_id"]


def test_stale_plan_is_rejected_after_main_advances(client: TestClient):
    run_id = f"stale_{uuid.uuid4().hex[:8]}"
    base = [row(1, "a", "x", 1)]
    dev = [row(1, "a", "y", 1)]
    main = [row(1, "a", "x", 1)]
    _seed_history(client, base, dev, main, run_id)
    plan = client.post("/api/v1/merges/plan", json={"dev": {"branch": "dev"}},
                       headers={"X-Request-ID": run_id}).json()

    # 再往 main 追加一个提交（模拟别人推进了主分支）
    newer = _ingest(client, [row(1, "a", "z", 2)], f"{run_id}-newer")
    pushed = client.post("/api/v1/branches/main/commits",
                         json={"snapshot_id": newer, "message": "another main commit"},
                         headers={"X-Request-ID": run_id})
    assert pushed.status_code == 201

    # 用旧 plan_id 解决/提交必须明确报过期，不能静默拿旧判定合并新头
    resp = client.post("/api/v1/merges/commit",
                       json={"plan_id": plan["plan_id"], "dev": {"branch": "dev"}},
                       headers={"X-Request-ID": run_id})
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "CONFLICT_STATE"
    assert "stale" in resp.json()["message"]


def test_schema_mismatch_returns_classified_422(client: TestClient):
    run_id = f"schema_{uuid.uuid4().hex[:8]}"
    base_id = _ingest(client, [row(1, "a", "x", 1)], run_id)
    init = client.post("/api/v1/repository/init",
                       json={"snapshot_id": base_id},
                       headers={"X-Request-ID": run_id})
    assert init.status_code == 201
    base_c = init.json()["commit_id"]
    client.post("/api/v1/branches", json={"name": "dev", "ref": {"commit_id": base_c}},
                headers={"X-Request-ID": run_id})

    other_schema = {
        "table": "employees",
        "columns": [{"name": "id", "type": "int64"}, {"name": "name", "type": "string"}],
        "primary_key": ["id"],
    }
    other = client.post("/api/v1/snapshots",
                        json={"schema": other_schema,
                              "rows": [{"id": 1, "name": "a"}]},
                        headers={"X-Request-ID": run_id})
    assert other.status_code == 201
    client.post("/api/v1/branches/dev/commits",
                json={"snapshot_id": other.json()["snapshot_id"]},
                headers={"X-Request-ID": run_id})

    resp = client.post("/api/v1/merges/plan", json={"dev": {"branch": "dev"}},
                       headers={"X-Request-ID": run_id})
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "SCHEMA_MISMATCH"
    assert resp.json()["details"]["problems"]


def test_bad_payloads_fail_with_codes_not_success(client: TestClient):
    # 重复初始化
    sid = _ingest(client, [row(1, "a", "x", 1)], "bad-init")
    assert client.post("/api/v1/repository/init", json={"snapshot_id": sid}).status_code == 201
    again = client.post("/api/v1/repository/init", json={"snapshot_id": sid})
    assert again.status_code == 409 and again.json()["error_code"] == "CONFLICT_STATE"

    # 摄取非法数据（bool 塞进 int）-> 422 SNAPSHOT_FORMAT
    bad_rows = [{"id": True, "name": "a", "city": "x", "score": 1.0, "active": True}]
    resp = client.post("/api/v1/snapshots", json={"schema": EMP_SCHEMA, "rows": bad_rows})
    assert resp.status_code == 422 and resp.json()["error_code"] == "SNAPSHOT_FORMAT"

    # 不存在的引用 -> 404
    resp = client.post("/api/v1/merges/plan",
                       json={"dev": {"commit_id": "commit_nope"}})
    assert resp.status_code == 404 and resp.json()["error_code"] == "NOT_FOUND"

    # 请求校验失败（Pydantic）-> 422 但不伪装成功
    resp = client.post("/api/v1/snapshots", json={"schema": EMP_SCHEMA, "rows": "nope"})
    assert resp.status_code == 422


def test_run_id_is_generated_when_absent(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"].startswith("run_")
    assert resp.json()["version"]


def test_resolution_bound_to_different_snapshot_set_is_rejected(client: TestClient):
    """冲突解决必须绑定当前三方快照：直接写入属于其他快照三元组的解决记录，

    提交时必须被识别并拒绝（INVALID_RESOLUTION），不能静默采用跨计划的决定。
    """
    run_id = f"bind_{uuid.uuid4().hex[:8]}"
    base = [row(4, "d", "hz", 40)]
    dev = []                                        # dev 删除 4
    main = [row(4, "d", "hf", 41)]                 # main 修改 4
    _seed_history(client, base, dev, main, run_id)
    plan = client.post("/api/v1/merges/plan", json={"dev": {"branch": "dev"}},
                       headers={"X-Request-ID": run_id}).json()
    plan_id = plan["plan_id"]

    # 绕过 /resolve，直接在元数据库里写入“绑定到别的快照”的解决记录
    from table_merge.storage import MetadataStore
    store: MetadataStore = client.app.state.service.store
    store.save_resolutions(plan_id, [{
        "row_key": "[4]",
        "decision": "DELETE_MODIFY_CONFLICT",
        "action": "KEEP_DELETED",
        "field_picks": None,
        "base_snapshot_id": "snap_OTHER_BASE",
        "dev_snapshot_id": "snap_OTHER_DEV",
        "main_snapshot_id": "snap_OTHER_MAIN",
    }])

    resp = client.post("/api/v1/merges/commit",
                       json={"plan_id": plan_id, "dev": {"branch": "dev"}},
                       headers={"X-Request-ID": run_id})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "INVALID_RESOLUTION"
    assert body["details"]["resolution_bound_to"]["base"] == "snap_OTHER_BASE"
    assert body["details"]["plan"]["base_snapshot_id"] == plan["base_snapshot_id"]
