"""HTTP 接口测试：状态码与错误类别、请求标识透传、只读视图与内核一致。"""

import json

import pytest
from fastapi.testclient import TestClient

from reorgindex.api import create_app
from reorgindex.config import Settings
from reorgindex.diagnostics import JsonDiagnostics
from reorgindex.storage import Storage

from .conftest import make_tx


@pytest.fixture()
def client(tmp_path, make):
    storage = Storage(":memory:")
    diagnostics = JsonDiagnostics(level="WARNING")
    app = create_app(
        settings=Settings(db_path=":memory:", finality_depth=6),
        storage=storage, diagnostics=diagnostics,
    )
    with TestClient(app) as c:
        c.make_block = make  # type: ignore[attr-defined]
        yield c
    storage.close()


def _submit(client, block, request_id=None):
    headers = {"X-Request-ID": request_id} if request_id else {}
    return client.post("/blocks", json=block, headers=headers)


def test_health_and_empty_state(client):
    assert client.get("/health").json() == {"ok": True, "service": "reorgindex"}
    state = client.get("/chain/state").json()
    assert state["tip_hash"] is None
    assert state["finality_depth"] == 6


def test_submit_genesis_and_extend(client, keys):
    g = client.make_block("0" * 64, 0, [], 1)
    r = _submit(client, g, "req-1")
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "req-1"
    assert r.json()["outcome"]["status"] == "extended"

    t = make_tx(keys["alice"][0], keys["alice"][1], keys["alice"][2], 12, 0)
    b1 = client.make_block(g["header"]["block_hash"], 1, [t], 1)
    r = _submit(client, b1, "req-2")
    assert r.status_code == 200
    bal = client.get(f"/balances/{keys['alice'][2]}").json()
    assert bal["balance"] == 12


def test_unknown_parent_returns_202_pending(client):
    orphan = client.make_block("ab" * 32, 1, [], 1)
    r = _submit(client, orphan, "req-pend")
    assert r.status_code == 202
    body = r.json()
    assert body["outcome"]["status"] == "pending"
    assert body["outcome"]["pending_count"] == 1
    assert body["request_id"] == "req-pend"


def test_bad_signature_returns_422_with_category(client, keys):
    g = client.make_block("0" * 64, 0, [], 1)
    _submit(client, g)
    b1 = client.make_block(g["header"]["block_hash"], 1, [], 1)
    b1["header"]["signature"] = "00" * 64
    r = _submit(client, b1, "req-bad")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["category"] == "verification_error"
    assert r.json()["request_id"] == "req-bad"


def test_malformed_body_returns_400(client):
    r = client.post("/blocks", json={"header": "nope", "txs": []})
    assert r.status_code == 400
    assert r.json()["error"]["category"] == "decode_error"


def test_duplicate_block_returns_409(client):
    g = client.make_block("0" * 64, 0, [], 1)
    assert _submit(client, g).status_code == 200
    r = _submit(client, g)
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "duplicate_block"


def test_finality_reorg_returns_409_and_state_unchanged(client, keys):
    D = 6
    g = client.make_block("0" * 64, 0, [], 1)
    _submit(client, g)
    cur = g
    for h in range(1, D + 2):
        cur = client.make_block(cur["header"]["block_hash"], h, [], 1)
        _submit(client, cur)
    tip_before = client.get("/chain/state").json()["tip_hash"]
    deep = client.make_block(g["header"]["block_hash"], 1, [], 10000)
    r = _submit(client, deep, "req-deep")
    assert r.status_code == 409
    assert r.json()["error"]["category"] == "finality_reorg"
    assert r.json()["error"]["context"]["rollback_count"] == D + 1
    assert client.get("/chain/state").json()["tip_hash"] == tip_before


def test_confirmations_and_finalization_endpoint(client):
    D = 6
    g = client.make_block("0" * 64, 0, [], 1)
    _submit(client, g)
    chain = [g]
    for h in range(1, D + 1):
        chain.append(client.make_block(chain[-1]["header"]["block_hash"], h, [], 1))
        _submit(client, chain[-1])
    gh = g["header"]["block_hash"]
    r = client.get(f"/chain/blocks/{gh}/confirmations").json()
    assert r["confirmation_depth"] == D + 1
    assert r["finalized"] is True
    tip_h = chain[-1]["header"]["block_hash"]
    r = client.get(f"/chain/blocks/{tip_h}/confirmations").json()
    assert r["confirmation_depth"] == 1
    assert r["finalized"] is False


def test_diagnostics_endpoint_records_request_id(client):
    g = client.make_block("0" * 64, 0, [], 1)
    _submit(client, g, "req-diag")
    events = client.get("/diagnostics?limit=50").json()["events"]
    accepted = [e for e in events if e["event"] == "block_accepted"]
    assert accepted
    assert accepted[0]["request_id"] == "req-diag"
    assert accepted[0]["payload"]["height"] == 0


def test_reorg_endpoint_records_rollback_range(client, keys):
    g = client.make_block("0" * 64, 0, [], 1)
    _submit(client, g)
    a1 = client.make_block(g["header"]["block_hash"], 1,
                           [make_tx(keys["alice"][0], keys["alice"][1], keys["alice"][2], 10, 0)], 1)
    _submit(client, a1)
    a2 = client.make_block(a1["header"]["block_hash"], 2,
                           [make_tx(keys["alice"][0], keys["alice"][1], keys["alice"][2], 20, 1)], 1)
    _submit(client, a2)
    f2 = client.make_block(a1["header"]["block_hash"], 2,
                           [make_tx(keys["alice"][0], keys["alice"][1], keys["bob"][2], 5, 2)], 3)
    r = _submit(client, f2, "req-reorg")
    assert r.json()["outcome"]["status"] == "switched"
    reorgs = client.get("/reorgs").json()["reorgs"]
    assert len(reorgs) == 1
    assert reorgs[0]["request_id"] == "req-reorg"
    assert reorgs[0]["rollback_from_height"] == 2
    assert reorgs[0]["rollback_to_height"] == 2
    assert len(reorgs[0]["disconnected"]) == 1


def test_rebuild_check_endpoint_ok(client):
    g = client.make_block("0" * 64, 0, [], 1)
    _submit(client, g)
    b1 = client.make_block(g["header"]["block_hash"], 1, [], 1)
    _submit(client, b1)
    r = client.post("/debug/rebuild-check")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_redacted_logs_do_not_leak_addresses(client, keys, capsys, caplog):
    # 构造一个带地址的提交并确认诊断序列化层对敏感字段脱敏（单测 scrub 行为）。
    from reorgindex.diagnostics import redact, scrub

    addr = keys["alice"][2]
    masked = redact(addr, keep=6)
    assert masked.startswith(addr[:6]) and masked.endswith(addr[-6:])
    assert addr not in masked
    out = scrub({"recipient": addr, "nested": {"address": addr}, "keep": 1}, keep=6)
    assert all(addr not in str(v) for v in [out["recipient"], out["nested"]["address"]])
    assert out["keep"] == 1
