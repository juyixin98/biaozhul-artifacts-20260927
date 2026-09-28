"""真实 HTTP 端到端：起 uvicorn + httpx，两个客户端经网络协作。

与进程内测试分开：这里验证 FastAPI 路由、JSON 线上格式、Idempotency-Key
头、HTTP 状态码与错误类别在真实套接字上都成立。
"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.api import create_app
from app.client import HTTPTransport, LocalClient
from app.config import Settings
from app.service import OTService
from app.storage import Storage


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    storage = Storage(str(tmp_path / "http.db"))
    svc = OTService(storage, Settings(db_path=":memory:", max_doc_chars=500))
    app = create_app(svc)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", lifespan="off")
    server_obj = uvicorn.Server(config)
    thread = threading.Thread(target=server_obj.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    # 等待端口就绪
    for _ in range(100):
        try:
            httpx.get(base_url + "/diagnostics", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    yield base_url, svc
    server_obj.should_exit = True
    thread.join(timeout=5)


def test_two_http_clients_converge(server):
    base_url, svc = server
    t = HTTPTransport(base_url)
    t.create_document("doc", "start")
    c1 = LocalClient("alpha", "doc", t)
    c2 = LocalClient("beta", "doc", t)
    c1.join(); c2.join()
    assert c1.text == c2.text == "start"

    c1.insert(0, "[")
    c2.insert(len(c2.text), "]")
    c1.sync()
    c2.sync()
    c1.sync()
    c2.sync()
    assert c1.text == c2.text == "[start]"
    assert c1.revision == c2.revision == 2  # 预置文本是 rev0，两笔插入→rev2
    t.close()


def test_http_error_categories_and_status(server):
    base_url, _ = server
    with httpx.Client(base_url=base_url, timeout=5) as h:
        # 404 input_error
        r = h.get("/documents/missing")
        assert r.status_code == 404
        assert r.json()["error"]["category"] == "input_error"

        # 建文档
        r = h.post("/documents", json={"doc_id": "d", "text": ""})
        assert r.status_code == 201

        # 400 input_error：坏操作（retain 超过空基线）
        r = h.post("/documents/d/ops", json={
            "client_id": "c", "client_seq": 1, "base_revision": 0,
            "components": [{"type": "retain", "n": 3}]})
        assert r.status_code == 400
        body = r.json()["error"]
        assert body["category"] == "input_error"
        assert body["code"] == "empty_insert"

        # 409 state_conflict：版本超前
        r = h.post("/documents/d/ops", json={
            "client_id": "c", "client_seq": 1, "base_revision": 9,
            "components": [{"type": "insert", "text": "hi",
                            "client_id": "c", "seq": 1}]})
        assert r.status_code == 409
        assert r.json()["error"]["category"] == "state_conflict"

        # 幂等：同 key 同体重放 → 200 且 replay
        payload = {"client_id": "c", "client_seq": 2, "base_revision": 0,
                   "components": [{"type": "insert", "text": "idem",
                                   "client_id": "c", "seq": 2}]}
        r1 = h.post("/documents/d/ops", json=payload,
                    headers={"Idempotency-Key": "k1"})
        r2 = h.post("/documents/d/ops", json=payload,
                    headers={"Idempotency-Key": "k1"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["revision"] == r2.json()["revision"] == 1
        assert r2.json()["replay"] is True


def test_http_stale_baseline_after_prune_410(server):
    base_url, svc = server
    t = HTTPTransport(base_url)
    t.create_document("p", "")
    with httpx.Client(base_url=base_url, timeout=5) as h:
        for i in range(1, 4):
            # 末尾追加单字符：前导 retain(i-1)，插入后无残留基线字符
            components = []
            if i - 1:
                components.append({"type": "retain", "n": i - 1})
            components.append({"type": "insert", "text": "x",
                               "client_id": "c", "seq": i})
            r = h.post("/documents/p/ops", json={
                "client_id": "c", "client_seq": i, "base_revision": i - 1,
                "components": components})
            assert r.status_code == 200, r.text
        assert h.get("/documents/p").json()["text"] == "xxx"
        r = h.post("/documents/p/prune", params={"new_horizon": 2})
        assert r.status_code == 200
        # 旧基线 rev0 拉取 → 410
        r = h.get("/documents/p/ops", params={"after": 0})
        assert r.status_code == 410
        assert r.json()["error"]["code"] == "stale_baseline"
    t.close()
