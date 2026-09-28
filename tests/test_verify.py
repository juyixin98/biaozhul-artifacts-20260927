"""验证接口 /verify 测试：守恒、切块不变性、独立核对。"""

from __future__ import annotations

import json

import numpy as np


def _payload(sig, **over):
    p = {
        "samples": [float(x) for x in sig],
        "sample_rate": 1000,
        "enter_threshold": 0.02,
        "exit_threshold": 0.05,
        "min_silence_ms": 300,
        "min_activity_ms": 100,
        "pad_ms": 50,
        "merge_gap_ms": 120,
        "chunk_sizes": [1, 7, 128, 4096],
    }
    p.update(over)
    return p


def test_verify_happy_path(client):
    sig = [0.5] * 100 + [0.0] * 600 + [0.5] * 100
    r = client.post("/verify", json=_payload(sig))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["raw_ranges"] == [[0, 100], [700, 800]]
    assert body["intervals"] == [[0, 150], [650, 800]]
    assert body["conservation"]["valid"] is True
    assert body["conservation"]["kept_samples"] == 300
    assert body["chunk_invariance"]["checked"] is True
    assert body["chunk_invariance"]["equal"] is True
    # 每个切块变体给出同样的具体区间
    for variant in body["chunk_invariance"]["variants"].values():
        assert variant == [[0, 150], [650, 800]]


def test_verify_all_silence(client):
    body = client.post("/verify", json=_payload([0.0] * 500)).json()
    assert body["ok"] is True
    assert body["intervals"] == []
    assert body["conservation"]["kept_samples"] == 0


def test_verify_detects_bad_threshold_order(client):
    sig = [0.0] * 10
    r = client.post(
        "/verify",
        json=_payload(sig, enter_threshold=0.9, exit_threshold=0.1),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_verify_rejects_nonfinite(client):
    # 用 content 发原始 JSON（JSON 文本里的 NaN 是常见宽容写法），
    # 服务端必须在信号内核处给 COMPUTATION_FAILED，而不是 500。
    payload = _payload([0.0, float("nan"), 0.5], pad_ms=0)
    r = client.post(
        "/verify",
        content=json.dumps(payload),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "COMPUTATION_FAILED"


def test_verify_chunk_invariance_field_without_variants(client):
    sig = [0.5] * 100
    body = client.post("/verify", json=_payload(sig, chunk_sizes=[])).json()
    assert body["chunk_invariance"]["checked"] is False
    assert body["chunk_invariance"]["equal"] is None
