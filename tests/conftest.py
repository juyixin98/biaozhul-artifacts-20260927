"""pytest 夹具：临时工作区 + TestClient + 可复用合成数据。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from deleter.api.app import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "ws")
    with TestClient(app) as c:
        c.ws_path = tmp_path / "ws"  # type: ignore[attr-defined]
        yield c


@pytest.fixture()
def service(tmp_path):
    from deleter.service import DeleterService
    svc = DeleterService(tmp_path / "ws")
    yield svc
    svc.close()


@pytest.fixture()
def make_table(client):
    """建一张 (id int64, name string, age int64) 主键 id 的表。"""
    def _make(table_id: str = "t1", key=None, columns=None):
        cols = columns or {"id": "int64", "name": "string", "age": "int64"}
        resp = client.post("/tables", json={
            "table_id": table_id, "columns": cols, "key": key or ["id"],
        })
        assert resp.status_code == 201, resp.text
        return resp.json()
    return _make


def load_inline(client, table_id: str, file_id: str, rows: list[dict]):
    resp = client.post(f"/tables/{table_id}/load", json={
        "file_id": file_id, "source": {"kind": "inline", "rows": rows},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def delete_batch(client, table_id: str, deletes: list[dict]):
    resp = client.post(f"/tables/{table_id}/deletes", json={"deletes": deletes})
    return resp


def snapshot(client, table_id: str) -> dict:
    resp = client.get(f"/tables/{table_id}/snapshot")
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def verdict_index(snap: dict) -> dict[tuple[str, int], dict]:
    return {(v["file_id"], v["row_number"]): v for v in snap["verdicts"]}
