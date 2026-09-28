"""FastAPI 服务层测试（TestClient，不起真实端口）。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import rsv.api.app as app_module
from rsv.config import load_settings
from tests.conftest import load_bundle


@pytest.fixture()
def client(tmp_path, monkeypatch, genesis):
    # 每个测试用独立 sqlite/runs 目录，避免模块级单例串状态
    settings = load_settings(
        sqlite_path=str(tmp_path / "api.sqlite3"),
        runs_dir=str(tmp_path / "runs"),
    )
    from rsv.chain import ChainKernel
    from rsv.storage import SQLiteStore

    monkeypatch.setattr(app_module, "settings", settings)
    monkeypatch.setattr(
        app_module, "kernel", ChainKernel(settings, SQLiteStore(settings.sqlite_path))
    )
    app_module.kernel.bootstrap_from_dict(genesis)
    return TestClient(app_module.app)

def test_health_reports_limits(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["network"] == "rsv-local"
    assert body["limits"]["max_op_steps"] == 128


def test_verify_then_submit_then_double_spend(client):
    bundle = load_bundle("double_spend.json")
    t1, t2 = bundle["transactions"]

    # dry-run 不改状态：重复 verify 两次都接受
    for _ in range(2):
        r = client.post("/tx/verify", json=t1)
        assert r.json()["accepted"] is True

    r = client.post("/tx/submit", json=t1)
    assert r.status_code == 200 and r.json()["accepted"] is True

    r = client.post("/tx/submit", json=t2)
    body = r.json()
    assert body["accepted"] is False
    assert body["failure"]["category"] == "state"
    assert body["failure"]["code"] == "state.already_spent"
    assert body["run_id"]  # 失败也有 run_id 可追查


def test_utxos_endpoint_and_state_root(client):
    r = client.get("/utxos")
    assert r.json()["count"] == 4
    r2 = client.get("/utxos", params={"domain": "rsv-other-domain-v2"})
    assert r2.json()["count"] == 1


def test_run_lookup_roundtrip(client):
    bundle = load_bundle("failure_catalog.json")
    r = client.post("/tx/submit", json=bundle["transactions"][5])  # unknown outpoint
    run_id = r.json()["run_id"]
    got = client.get(f"/runs/{run_id}").json()
    assert got["code"] == "state.unknown_outpoint"
    assert got["accepted"] is False
    assert got["reason"]


def test_replay_endpoint(client):
    bundle = load_bundle("failure_catalog.json")
    r = client.post("/replay", json=bundle)
    body = r.json()
    # 全部 6 笔都应失败（该 bundle 里没有可成功的前置交易）
    assert body["rejected_count"] == 6
    codes = [x.get("failure", {}).get("code") for x in body["results"] if "failure" in x]
    assert "compute.crypto.threshold_invalid" in codes
    assert "state.domain_conflict" in codes


def test_malformed_json_body(client):
    r = client.post("/tx/submit", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["failure"]["category"] == "input"
