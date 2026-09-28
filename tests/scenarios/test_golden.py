"""黄金场景：重写身份 / 先删后插 / 重复键 / 跨文件 / NULL。

参考答案是人工推导的 tests/fixtures/golden_canonical.json，
同时与独立预言机 tests/oracle/reference.py 做三方对照。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import delete_batch, snapshot, verdict_index
from oracle.reference import KERNEL_EQUIVALENCE, ReferenceTable

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden_canonical.json"


@pytest.fixture(scope="module")
def golden():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.scenario
def test_golden_scenario(client, golden):
    schema = golden["schema"]
    resp = client.post("/tables", json={
        "table_id": "g1", "columns": schema["columns"], "key": schema["key"],
    })
    assert resp.status_code == 201

    oracle = ReferenceTable(schema["key"])
    step_index = 0
    version_checks = {s["after_step"]: s for s in golden["version_snapshots"]}

    for step in golden["steps"]:
        step_index += 1
        if step["op"] == "load":
            r = client.post("/tables/g1/load", json={
                "file_id": step["file_id"],
                "source": {"kind": "inline", "rows": step["rows"]},
            })
            assert r.status_code == 201, r.text
            oracle.load(step["file_id"], step["rows"])

        elif step["op"] == "eq":
            r = delete_batch(client, "g1", [{
                "delete_id": step["delete_id"], "kind": "equality", "key": step["key"],
            }])
            if step.get("expect_http"):
                assert r.status_code == step["expect_http"], r.text
                assert r.json()["error"]["category"] == step["expect_category"]
                continue
            assert r.status_code == 200, r.text
            oracle.equality_delete(step["delete_id"], [step["key"][c] for c in schema["key"]])

        elif step["op"] == "pos":
            r = delete_batch(client, "g1", [{
                "delete_id": step["delete_id"], "kind": "position",
                "file_id": step["file_id"], "row_number": step["row_number"],
            }])
            if step.get("expect_http"):
                assert r.status_code == step["expect_http"], r.text
                body = r.json()
                assert body["error"]["category"] == step["expect_category"]
                # 被拒绝请求不得分配序列号（下一次成功操作的 seq 连续）
                continue
            assert r.status_code == 200, r.text
            oracle.position_delete(step["delete_id"], step["file_id"], step["row_number"])

        elif step["op"] == "rewrite":
            r = client.post("/tables/g1/rewrite", json={
                "file_ids": step["file_ids"], "new_file_id": step["new_file_id"],
            })
            assert r.status_code == 200, r.text
            oracle.rewrite(step["file_ids"], step["new_file_id"])

        # 逐版本快照断言
        if step_index in version_checks:
            _assert_version_snapshot(client, oracle, version_checks[step_index])

    # ---- 最终逐行依据（人工黄金答案） ----
    snap = snapshot(client, "g1")
    idx = verdict_index(snap)
    assert set(snap["table"]["live_files"]) == set(golden["final_live_files"])

    kept = deleted = 0
    for exp in golden["final_verdicts"]:
        got = idx[(exp["file_id"], exp["row_number"])]
        assert got["action"] == exp["action"], (exp, got)
        assert got["reason"] == exp["reason"], (exp, got)
        assert got["by_delete_id"] == exp["by_delete_id"], (exp, got)
        assert got["values"]["id"] == exp["id"], (exp, got)
        assert got["insert_seq"] == golden["seq_assignments"][_seq_key(exp)], (exp, got)
        kept += got["action"] == "keep"
        deleted += got["action"] == "delete"
    assert len(idx) == len(golden["final_verdicts"])
    assert (kept, deleted) == (golden["final_kept_rows"], golden["final_deleted_rows"])

    # ---- 与独立预言机逐行交叉验证 ----
    o_verdicts = oracle.scan_verdicts()
    assert len(o_verdicts) == len(snap["verdicts"])
    for ov, sv in zip(sorted(o_verdicts, key=lambda v: (v["file_id"], v["row_number"])),
                      sorted(snap["verdicts"], key=lambda v: (v["file_id"], v["row_number"]))):
        assert ov["file_id"] == sv["file_id"] and ov["row_number"] == sv["row_number"]
        assert ov["action"] == sv["action"], (ov, sv)
        assert KERNEL_EQUIVALENCE[ov["oracle_reason"]] == sv["reason"], (ov, sv)
        assert ov["by_delete_id"] == sv["by_delete_id"], (ov, sv)
        assert ov["insert_seq"] == sv["insert_seq"], (ov, sv)

    # 序列号人工答案
    deletes = {d["delete_id"]: d["seq"] for d in snap["deletes"]}
    loads = {f["file_id"]: f["created_seq"] for f in snap["files"] if f["version"] == 1}
    for name, want in golden["seq_assignments"].items():
        if not isinstance(want, int):
            continue
        if name.startswith("load "):
            assert loads[name.split(" ", 1)[1]] == want, name
        else:
            assert deletes[name] == want, name


def _seq_key(exp: dict) -> str:
    """期望行的 insert_seq 应对应哪次载入。"""
    return {"fA": "load fA", "fB": "load fB",
            "fC": "load fA", "fD": "load fD"}[exp["file_id"]]


def _assert_version_snapshot(client, oracle, spec):
    snap = snapshot(client, "g1")
    live = {f["file_id"]: f["version"] for f in snap["files"] if f["is_live"]}
    assert live == spec["live"], (spec, live)
    idx = verdict_index(snap)
    for fid, rn in spec.get("expect_deleted", []) + spec.get("expect_deleted_after_rewrite", []):
        assert idx[(fid, rn)]["action"] == "delete", (spec, fid, rn, idx[(fid, rn)])

    # 每个中间版本也必须与预言机一致
    o_verdicts = oracle.scan_verdicts()
    for ov in o_verdicts:
        sv = idx[(ov["file_id"], ov["row_number"])]
        assert KERNEL_EQUIVALENCE[ov["oracle_reason"]] == sv["reason"], (ov, sv)
        assert ov["action"] == sv["action"], (ov, sv)
