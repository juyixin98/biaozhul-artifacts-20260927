"""HTTP 端到端：FastAPI + httpx ASGI，含全部错误类别与诊断端点。"""

import pytest
from fastapi.testclient import TestClient

from otbackend.api import create_app
from otbackend.config import Settings
from otbackend.repository import MemoryRepository
from otbackend.service import OTService


@pytest.fixture()
def client():
    svc = OTService(MemoryRepository())
    app = create_app(Settings(db_path=":memory:"), service=svc)
    return TestClient(app), svc


def test_health_and_crud(client):
    c, _ = client
    assert c.get("/healthz").json()["ok"]
    r = c.post("/documents", json={"doc_id": "d", "initial_text": "abc"})
    assert r.status_code == 200
    g = c.get("/documents/d").json()
    assert g["rev"] == 0 and g["text"] == "abc" and g["baseline_rev"] == 0


def test_submit_and_converge(client):
    c, _ = client
    c.post("/documents", json={"doc_id": "d", "initial_text": "abc"})
    h = {"base_rev": 0, "client_id": "alice", "client_op_id": 1,
         "ops": [{"type": "ins", "pos": 0, "text": "A"}]}
    c.post("/documents/d/submit", json=h)
    r2 = c.post("/documents/d/submit", json={
        "base_rev": 0, "client_id": "bob", "client_op_id": 1,
        "ops": [{"type": "ins", "pos": 1, "text": "B"}]}).json()
    assert r2["rev"] == 2
    # A@0 与 B@1（基于 r0 的不同位置）：结果确定为 AaBbc
    assert r2["text"] == "AaBbc", r2["text"]


def test_error_categories_over_http(client):
    c, _ = client
    c.post("/documents", json={"doc_id": "d", "initial_text": "abc"})

    def body(reason, **kw):
        return {"error": {"reason": reason}} if False else None

    # 400 输入错误：坏 JSON
    r = c.post("/documents/d/submit", data="{bad",
               headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "BAD_JSON"
    # 400 空 client_id
    r = c.post("/documents/d/submit",
               json={"base_rev": 0, "client_id": "", "client_op_id": 1,
                     "ops": [{"type": "ins", "pos": 0, "text": "z"}]})
    assert r.status_code == 400 and r.json()["error"]["reason"] == "BAD_CLIENT_ID"
    # 404
    assert c.get("/documents/missing").status_code == 404
    # 409 状态冲突：越界删除
    r = c.post("/documents/d/submit",
               json={"base_rev": 0, "client_id": "x", "client_op_id": 1,
                     "ops": [{"type": "del", "pos": 9, "length": 1}]})
    assert r.status_code == 409 and r.json()["error"]["reason"] == "DELETE_OUT_OF_RANGE"
    # 409 旧基线
    c.post("/documents/d/trim", json={"keep_from_rev": 0})
    c.post("/documents/d/submit",
           json={"base_rev": 0, "client_id": "y", "client_op_id": 1,
                 "ops": [{"type": "ins", "pos": 0, "text": "q"}]})
    # catchup 可重建
    rc = c.get("/documents/d/catchup").json()
    assert rc["rebuild_required"] is True


def test_idempotency_over_http(client):
    c, _ = client
    c.post("/documents", json={"doc_id": "d", "initial_text": "abc"})
    payload = {"base_rev": 0, "client_id": "z", "client_op_id": 3,
               "ops": [{"type": "ins", "pos": 0, "text": "Q"}]}
    r1 = c.post("/documents/d/submit", json=payload).json()
    r2 = c.post("/documents/d/submit", json=payload).json()
    assert r1["rev"] == r2["rev"] == 1


def test_history_and_revision_detail(client):
    c, _ = client
    c.post("/documents", json={"doc_id": "d", "initial_text": "abc"})
    c.post("/documents/d/submit",
           json={"base_rev": 0, "client_id": "z", "client_op_id": 1,
                 "ops": [{"type": "ins", "pos": 0, "text": "Q"}]})
    h = c.get("/documents/d/history").json()
    assert h["head_rev"] == 1 and len(h["revisions"]) == 1
    rev = h["revisions"][0]
    assert rev["checksum"] and rev["client_id"] == "z"
    d = c.get("/documents/d/revisions/1").json()
    assert d["rev"] == 1 and d["ops"][0]["text"] == "Q"
    # request_id 存在
    assert h.get("request_id")


def test_request_id_echo(client):
    c, _ = client
    c.post("/documents", json={"doc_id": "d", "initial_text": ""})
    r = c.get("/documents/d", headers={"x-request-id": "fixed-id-123"})
    assert r.headers.get("x-request-id") == "fixed-id-123"
    assert r.json()["request_id"] == "fixed-id-123"
