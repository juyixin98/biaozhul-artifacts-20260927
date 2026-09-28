"""HTTP API 端到端测试（FastAPI TestClient，无需起真实端口）。"""

from __future__ import annotations

import importlib
import os

import pytest

# 每个 API 测试用独立临时 DB，避免污染
os.environ["ABI_DB_PATH"] = ":memory:"

from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "api.db"
    monkeypatch.setenv("ABI_DB_PATH", str(db_file))
    # 配置与 API 模块在导入时读取了 settings，需整体重载以使用临时文件 DB。
    import app.config
    import app.storage
    import app.api as api_mod
    importlib.reload(app.config)
    importlib.reload(app.storage)
    importlib.reload(api_mod)
    with TestClient(api_mod.app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "version" in body
    assert "x-run-id" in r.headers


def test_encode_decode_roundtrip(client):
    r = client.post("/abi/encode", json={
        "types": ["(string,uint256)"], "values": [["hello", 42]]
    })
    assert r.status_code == 200, r.text
    blob = r.json()["data"]
    d = client.post("/abi/decode", json={"types": ["(string,uint256)"], "data": blob})
    assert d.status_code == 200
    assert d.json()["values"] == [["hello", 42]]


def test_selector_known(client):
    r = client.post("/abi/selector", json={"signature": "transfer(address,uint256)"})
    assert r.json()["selector"] == "0xa9059cbb"


def test_decode_bad_padding_returns_category_not_200(client):
    bad = "0x" + "01" + "00" * 31  # uint8 非规范高位
    r = client.post("/abi/decode", json={"types": ["uint8"], "data": bad})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["category"] == "non_canonical_padding"


def test_huge_length_rejected(client):
    blob = "0x" + "00" * 31 + "20" + "ff" * 32
    r = client.post("/abi/decode", json={"types": ["bytes"], "data": blob})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "allocation_limit"


def test_chain_transact_ok_and_revert_indexed(client):
    # 成功铸造
    r = client.post("/chain/transact", json={
        "signature": "mint(bytes20,uint256)",
        "args": [{"bytes": "0x" + "aa" * 20}, 100],
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    tx_ok = r.json()["receipt"]["tx_hash"]

    # 余额不足转账 → ok=false 但 HTTP 200（业务回滚，非请求错误），带类别
    r2 = client.post("/chain/transact", json={
        "signature": "transfer(bytes20,bytes20,uint256)",
        "args": [{"bytes": "0x" + "bb" * 20}, {"bytes": "0x" + "aa" * 20}, 9],
    })
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is False
    assert body["receipt"]["status"] == "reverted"
    assert body["receipt"]["error_category"] == "insufficient_balance"

    # 索引可查成功交易
    g = client.get(f"/chain/tx/{tx_ok}")
    assert g.status_code == 200 and g.json()["transaction"]["status"] == "ok"


def test_unknown_tx_404(client):
    r = client.get("/chain/tx/0xabc")
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "not_found"


def test_block_endpoint(client):
    r = client.post("/chain/block", json={"calldata": []})
    assert r.status_code == 200
    assert r.json()["block_number"] == 0
