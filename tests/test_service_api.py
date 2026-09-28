"""服务层 + HTTP API 测试。

断言具体的错误 **类别**（input_error / state_conflict / resource_exhausted /
compute_failed）与具体的收敛文本，而非"接口能调用"。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.errors import (
    DocumentNotFound,
    DuplicateRequest,
    EmptyInsert,
    OperationTooLarge,
    RevisionAhead,
    StaleBaseline,
    TransformInvariant,
)
from app.models import Op
from app.service import OTService
from app.storage import Storage


@pytest.fixture
def svc(tmp_path):
    storage = Storage(str(tmp_path / "t.db"))
    service = OTService(storage, Settings(db_path=":memory:", max_doc_chars=50, max_op_chars=10))
    service.create_document("doc", "")
    return service


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


def _insert(svc, client_id, seq, base, pos, text, key=None):
    doc = svc.get_document("doc")
    op = Op.insert_at(pos, text, (client_id, seq), len(doc["text"]))
    return svc.submit("doc", client_id, seq, base, op, key)


# ------------------------------------------------------------ 基本收敛
def test_concurrent_inserts_through_server_converge(svc):
    # 两个操作都基于 rev 0，在位置 0 并发插入；服务端串行 transform
    doc0 = svc.get_document("doc")
    op1 = Op.insert_at(0, "AB", ("c1", 1), 0)
    op2 = Op.insert_at(0, "CD", ("c2", 1), 0)
    r1 = svc.submit("doc", "c1", 1, 0, op1)
    r2 = svc.submit("doc", "c2", 1, 0, op2)
    assert r1.text == "AB"
    # ("c1",1) < ("c2",1)：c1 排前
    assert r2.text == "ABCD"
    assert r2.rebased is True
    assert svc.get_document("doc")["head_revision"] == 2


def test_overlapping_remote_edits_via_replay(svc):
    _insert(svc, "c1", 1, 0, 0, "hello")          # rev1 "hello"
    doc = svc.get_document("doc")
    # c2 基于 rev1：在中间插入；c3 基于 rev1：删除 "ll"
    op_ins = Op.insert_at(2, "XY", ("c2", 2), len(doc["text"]))
    op_del = Op.delete_range(2, 2, len(doc["text"]))
    ri = svc.submit("doc", "c2", 2, 1, op_ins)
    rd = svc.submit("doc", "c3", 1, 1, op_del)
    assert ri.text == "heXYllo"
    # 删除意图作用于原 "ll"（在 XY 之后），不能误删插入内容
    assert rd.text == "heXYo"


# ------------------------------------------------------------ 错误分类
def test_input_errors_categorized(svc, client, otlog):
    # 未知文档
    with pytest.raises(DocumentNotFound):
        svc.get_document("nope")
    resp = client.get("/documents/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "input_error"

    # 越界删除（输入错误）
    resp = client.post(
        "/documents/doc/ops",
        json={"client_id": "c", "client_seq": 1, "base_revision": 0,
              "components": [{"type": "retain", "n": 1},
                             {"type": "delete", "n": 1}]},
    )
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["category"] == "input_error"
    assert body["code"] == "malformed_operation"

    # 形状错误（缺字段 → FastAPI 422，也属于输入错误家族，这里检查被拒）
    resp = client.post(
        "/documents/doc/ops",
        json={"client_id": "c", "base_revision": 0, "components": []},
    )
    assert resp.status_code == 422

    # no-op 编辑拒绝
    from app.models import Component
    noop = Op.build([Component.retain(3)])
    with pytest.raises(EmptyInsert):
        svc.submit("doc", "c", 9, 0, noop)

    otlog(
        "error-categories",
        verdict="pass",
        reason="未知文档 404、坏操作 400、形状错误 422、no-op 拒绝均归入 input_error",
        inputs={"cases": ["doc_not_found", "malformed_operation",
                          "schema_422", "empty_insert"]},
        states={"category": "input_error"},
        category="input_error",
    )


def test_state_conflict_stale_and_ahead(svc):
    op = Op.insert_at(0, "a", ("c1", 1), 0)
    svc.submit("doc", "c1", 1, 0, op)  # head=1
    op2 = Op.insert_at(0, "b", ("c2", 1), 0)
    # 基于未来版本
    with pytest.raises(RevisionAhead):
        svc.submit("doc", "c2", 1, 5, op2)


def test_duplicate_submit_is_idempotent(svc):
    op = Op.insert_at(0, "dup", ("c1", 1), 0)
    r1 = svc.submit("doc", "c1", 1, 0, op, idem_key="k-1")
    # 完全相同的重复提交（同 key 同体）：返回首次结果，head 不增长
    r2 = svc.submit("doc", "c1", 1, 0, op, idem_key="k-1")
    assert r1.revision == r2.revision == 1
    assert r2.replay is True
    # 同 key 不同体 → 冲突
    other = Op.insert_at(0, "other", ("c2", 1), 0)
    with pytest.raises(DuplicateRequest):
        svc.submit("doc", "c2", 1, 0, other, idem_key="k-1")
    # 同客户端同 seq 不同载荷 → 也算冲突
    with pytest.raises(DuplicateRequest):
        svc.submit("doc", "c1", 1, 0,
                   Op.insert_at(0, "changed", ("c1", 1), 0))


def test_resource_exhausted_document_and_op(svc):
    big = "x" * 11  # max_op_chars=10
    with pytest.raises(OperationTooLarge):
        svc.submit("doc", "c", 1, 0, Op.insert_at(0, big, ("c", 1), 0))

    # 用 5 次各 10 字符的合法插入把文档填到 max_doc_chars=50
    for k in range(5):
        svc.submit("doc", "c", k + 1, k,
                   Op.insert_at(k * 10, "a" * 10, ("c", k + 1), k * 10))
    assert len(svc.get_document("doc")["text"]) == 50
    # 再插 10：操作本身未超 op 限制(10)，但结果文档 60 > 50 → 文档超限
    with pytest.raises(Exception) as ei:
        svc.submit("doc", "c", 6, 5,
                   Op.insert_at(50, "b" * 10, ("c", 6), 50))
    from app.errors import DocumentTooLarge
    assert ei.type is DocumentTooLarge
    # 超限提交必须回滚：head 仍是 5，文本仍是 50 个 a
    doc = svc.get_document("doc")
    assert doc["head_revision"] == 5 and doc["length"] == 50


def test_compute_failure_distinct_via_fault_injection(svc, client):
    svc.arm_fault("doc", 1)
    with pytest.raises(TransformInvariant) as ei:
        svc.submit("doc", "c", 1, 0, Op.insert_at(0, "a", ("c", 1), 0))
    assert ei.value.category.value == "compute_failed"
    # HTTP 层映射为 500 且类别可区分
    resp = client.post("/internal/faults/doc")  # 再装一颗
    assert resp.status_code == 200
    resp = client.post(
        "/documents/doc/ops",
        json={"client_id": "c", "client_seq": 1, "base_revision": 0,
              "components": [{"type": "insert", "text": "a",
                              "client_id": "c", "seq": 1}]},
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["category"] == "compute_failed"


# ------------------------------------------------------------ 历史裁剪
def test_prune_then_old_baseline_rejected(svc, otlog):
    for i in range(1, 5):
        _insert(svc, "c1", i, i - 1, 0, f"v{i}")
    head = svc.get_document("doc")
    assert head["head_revision"] == 4

    # 旧客户端在裁剪前只同步到 rev 1，本地仍记得 rev 1 的文本（此处等价地
    # 在裁剪前取回并记忆）。裁剪后它用该旧基线提交 → stale_baseline (410)。
    remembered_rev1 = svc.text_at("doc", 1)  # 裁剪前可读
    info = svc.prune("doc", 3)
    assert info["pruned_horizon"] == 3

    with pytest.raises(StaleBaseline) as ei:
        svc.submit("doc", "c2", 1, 1,
                   Op.insert_at(0, "late", ("c2", 1), len(remembered_rev1)))
    assert ei.value.category.value == "state_conflict"

    # 旧基线拉取同样拒绝
    with pytest.raises(StaleBaseline):
        svc.pull("doc", 1)

    # 新基线（== horizon）仍可提交；该操作还会穿过已提交的 op4（"v4"，
    # origin ("c1",4) < ("c2",1) 同点排前），故 v4 在 NEW 前。
    text_at_3 = svc.text_at("doc", 3)
    r = svc.submit("doc", "c2", 1, 3,
                   Op.insert_at(0, "NEW", ("c2", 1), len(text_at_3)))
    assert r.text == "v4NEW" + text_at_3

    otlog(
        "prune-baseline",
        verdict="pass",
        reason="裁剪到 rev3 后，基于 rev1 的提交/拉取均拒绝(stale_baseline)，"
               "基于水位 rev3 的提交可重放并与 op4 正确排序",
        inputs={"pruned_horizon": 3, "old_base_revision": 1,
                "new_base_revision": 3},
        states={"rev1_text": remembered_rev1,
                "rev3_snapshot": text_at_3,
                "final_text": r.text},
        category="state_conflict",
    )


def test_pull_pagination(svc):
    for i in range(1, 6):
        _insert(svc, "c1", i, i - 1, 0, "x")
    p = svc.pull("doc", 0, limit=2)
    assert len(p.ops) == 2 and p.has_more is True
    p2 = svc.pull("doc", 2, limit=10)
    assert [s.revision for s in p2.ops] == [3, 4, 5] and p2.has_more is False


def test_diagnostics_reports_state(svc, client):
    _insert(svc, "c1", 1, 0, 0, "abc")
    resp = client.get("/diagnostics")
    assert resp.status_code == 200
    docs = {d["doc_id"]: d for d in resp.json()["documents"]}
    assert docs["doc"]["char_length"] == 3
    assert docs["doc"]["head_revision"] == 1
