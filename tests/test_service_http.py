"""FastAPI 服务端到端测试（httpx TestClient，真实 HTTP 层）。

断言具体状态码与失败类别；验证失败的响应必须保留 UTXO 状态（只返回分类）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from service import create_app
from stackvm.config import load_settings
from stackvm.runlog import RunLogger


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = load_settings()
    db = tmp_path / "svc.db"
    # 把运行日志重定向到临时目录，避免测试污染正式 runlogs（会话级夹具除外）
    app = create_app(settings, db_path=db, bootstrap=True,
                     runlog_kind="service-tests")
    with TestClient(app) as c:
        yield c, settings, db
    app.state.store.close()


def test_health_bootstrapped(client):
    c, _, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["height"] == 1
    assert len(body["state_root"]) == 64


def test_verify_endpoint_does_not_mutate_state(client, cases_doc):
    c, _, _ = client
    for cid in ["00", "02", "12", "20"]:
        r = c.post("/transactions/verify", json=cases_doc["cases"][cid]["tx"])
        assert r.status_code == 200, (cid, r.text)
        assert r.json()["accepted"] is True
    # verify 多少次 height 都不变
    assert c.get("/health").json()["height"] == 1
    assert len(c.get("/utxos").json()["utxos"]) == 21


def test_submit_success_and_state_transition(client, cases_doc):
    c, _, _ = client
    r = c.post("/transactions/submit", json=cases_doc["cases"]["20"]["tx"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["height"] == 2
    # 紧接双花 → 状态冲突
    r2 = c.post("/transactions/submit", json=cases_doc["cases"]["20"]["tx"])
    assert r2.status_code == 422
    assert r2.json()["code"] == "TX_ALREADY_ACCEPTED"
    assert r2.json()["kind"] == "STATE"


@pytest.mark.parametrize("cid,code,kind", [
    ("03", "STACK_UNDERFLOW", "COMPUTE"),
    ("05", "SIG_DUPLICATED", "COMPUTE"),
    ("06", "THRESHOLD_NOT_MET", "COMPUTE"),
    ("07", "SIG_INVALID", "COMPUTE"),
    ("08", "BUDGET_EXHAUSTED", "RESOURCE"),
    ("09", "STACK_UNDERFLOW", "COMPUTE"),
    ("11", "EVAL_FALSE", "COMPUTE"),
    ("15", "ELEMENT_TOO_LARGE", "INPUT"),
    ("16", "UNKNOWN_OPCODE", "INPUT"),
    ("17", "UNCLEAN_STACK", "COMPUTE"),
])
def test_submit_failures_categorized_and_no_transfer(client, cases_doc,
                                                     cid, code, kind):
    c, _, _ = client
    height_before = c.get("/health").json()["height"]
    r = c.post("/transactions/submit", json=cases_doc["cases"][cid]["tx"])
    assert r.status_code == 422 if kind != "INPUT" else r.status_code in (400, 422)
    body = r.json()
    assert body["accepted"] is False
    assert body["code"] == code
    assert body["kind"] == kind
    # 没有执行转账
    assert c.get("/health").json()["height"] == height_before
    assert len(c.get("/utxos").json()["utxos"]) == 21


def test_malformed_request_is_input_error(client):
    c, _, _ = client
    r = c.post("/transactions/verify", json={"version": 1, "inputs": [],
                                             "outputs": []})
    assert r.status_code == 400
    body = r.json()
    assert body["kind"] == "INPUT"
    assert body["code"] == "REQUEST_MALFORMED"


def test_bad_hex_is_input_error(client, cases_doc):
    c, _, _ = client
    tx = json.loads(json.dumps(cases_doc["cases"]["00"]["tx"]))
    tx["inputs"][0]["unlock"] = "zz"
    r = c.post("/transactions/verify", json=tx)
    assert r.status_code == 400
    assert r.json()["code"] in ("TX_MALFORMED", "REQUEST_MALFORMED")


def test_state_endpoint_runs_replay(client, cases_doc):
    c, _, _ = client
    c.post("/transactions/submit", json=cases_doc["cases"]["00"]["tx"])
    r = c.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert body["height"] == 2
    assert body["replay"]["ok"] is True
    assert body["replay"]["rebuilt_state_root"] == body["state_root"]


def test_journal_endpoint_lists_entries(client, cases_doc):
    c, _, _ = client
    c.post("/transactions/submit", json=cases_doc["cases"]["04"]["tx"])
    r = c.get("/journal")
    assert r.status_code == 200
    entries = r.json()["journal"]
    assert entries[0]["kind"] == "GENESIS"
    assert entries[-1]["kind"] == "APPLY"
    # 链式哈希
    assert entries[1]["prev_hash"] == entries[0]["row_hash"]


def test_response_contains_run_id_for_replay(client, cases_doc, tmp_path):
    c, settings, _ = client
    r = c.post("/transactions/verify", json=cases_doc["cases"]["09"]["tx"])
    body = r.json()
    assert body["code"] == "STACK_UNDERFLOW"
    run_id = body["run_id"]
    # 运行日志落盘，含 trace_step（关键中间状态）
    log_dir = settings.abspath(settings.storage.runlog_dir) / "service-tests" / run_id
    summary = json.loads((log_dir / "summary.json").read_text())
    events = (log_dir / "events.jsonl").read_text().splitlines()
    assert summary["verdict"] == "STACK_UNDERFLOW"
    assert any('"event": "trace_step"' in e for e in events)
    assert any('"event": "verdict"' in e for e in events)
