"""端到端 HTTP 测试：成功路径与各类拒绝/无法判定的具体错误响应。

用 FastAPI TestClient（基于 httpx），数据库全部是 tmp_path 下的本地合成夹具。
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from acstream.app import create_app
from acstream.config import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AC_CURSOR_SECRET", "unit-test-secret-key-0123456789ab")
    app = create_app(Settings.load())
    with TestClient(app) as c:
        yield c


def create_version(client, patterns, encoding="utf-8") -> dict:
    r = client.post("/versions", json={"encoding": encoding, "patterns": patterns})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ 成功路径

def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_oneshot_nested_overlapping(client) -> None:
    r = client.post(
        "/match",
        json={
            "encoding": "utf-8",
            "data": "aaaa",
            "patterns": [
                {"id": "a", "data": "a"},
                {"id": "aa", "data": "aa"},
                {"id": "aaa", "data": "aaa"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    hits = sorted((h["pattern_id"], h["start"], h["end"]) for h in r.json()["hits"])
    assert hits == sorted(
        [("a", i, i + 1) for i in range(4)]
        + [("aa", i, i + 2) for i in range(3)]
        + [("aaa", i, i + 3) for i in range(2)]
    )


def test_stream_cross_chunk_absolute_offsets(client) -> None:
    v = create_version(client, [{"id": "sig", "data": "abcd"}])
    sid = client.post("/sessions", json={"version_id": v["version_id"]}).json()["sid"]

    r1 = client.post(f"/sessions/{sid}/feed", json={"data": "xxab"})
    assert r1.status_code == 200 and r1.json()["new_hits"] == 0

    r2 = client.post(
        f"/sessions/{sid}/feed",
        json={"data": "cdxx", "expected_offset": 4},
    )
    assert r2.json()["new_hits"] == 1
    hits = client.get(f"/sessions/{sid}/hits").json()["items"]
    assert [(h["pattern_id"], h["start"], h["end"]) for h in hits] == [
        ("sig", 2, 6)
    ]


def test_binary_base64_roundtrip(client) -> None:
    raw = b"\x00\xff\xde\xad\xbe\xef\x00"
    # 同时验证 /versions 接受二进制 base64 模式（版本级编码）。
    create_version(
        client,
        [
            {"id": "h", "data": base64.b64encode(b"\xde\xad\xbe\xef").decode()},
            {"id": "nul", "data": base64.b64encode(b"\x00").decode()},
        ],
        encoding="base64",
    )
    r = client.post(
        "/match",
        json={"encoding": "base64", "data": base64.b64encode(raw).decode(),
              "patterns": [
                  {"id": "h", "data": base64.b64encode(b"\xde\xad\xbe\xef").decode()},
                  {"id": "nul", "data": base64.b64encode(b"\x00").decode()},
              ]},
    )
    assert r.status_code == 200, r.text
    hits = sorted((h["pattern_id"], h["start"], h["end"]) for h in r.json()["hits"])
    assert hits == [("h", 2, 6), ("nul", 0, 1), ("nul", 6, 7)]


def test_switch_version_end_to_end(client) -> None:
    v1 = create_version(client, [{"id": "ab", "data": "ab"}])
    v2 = create_version(client, [{"id": "bc", "data": "bc"}])
    sid = client.post("/sessions", json={"version_id": v1["version_id"]}).json()["sid"]
    client.post(f"/sessions/{sid}/feed", json={"data": "a"})
    r = client.post(
        f"/sessions/{sid}/switch-version", json={"version_id": v2["version_id"]}
    )
    assert r.status_code == 200
    assert r.json()["state"]["node_state"] == 0
    assert r.json()["state"]["byte_offset"] == 1


def test_request_id_is_generated_and_echoed(client) -> None:
    r = client.get("/health", headers={"X-Request-ID": "fixed-req-123"})
    assert r.headers["x-request-id"] == "fixed-req-123"


# -------------------------------------------------------------- 拒绝/无法判定

def test_empty_pattern_rejected_with_code(client) -> None:
    r = client.post(
        "/versions",
        json={"patterns": [{"id": "ok", "data": "a"}, {"id": "bad", "data": ""}]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "EMPTY_PATTERN"
    assert err["outcome"] == "reject"
    assert err["details"]["pattern_id"] == "bad"
    assert err["request_id"]


def test_empty_pattern_set_rejected(client) -> None:
    r = client.post("/versions", json={"patterns": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "EMPTY_PATTERN_SET"


def test_duplicate_id_rejected(client) -> None:
    r = client.post(
        "/versions",
        json={"patterns": [{"id": "d", "data": "ab"}, {"id": "d", "data": "cd"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_PATTERN_ID"


def test_bad_base64_is_encoding_error(client) -> None:
    r = client.post(
        "/match",
        json={"encoding": "base64", "data": "@@not-base64@@",
              "patterns": [{"id": "p", "data": "YQ=="}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ENCODING_ERROR"


def test_missing_version_is_404(client) -> None:
    r = client.post("/sessions", json={"version_id": "ver_nope"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "VERSION_NOT_FOUND"


def test_unknown_session_is_404(client) -> None:
    r = client.post("/sessions/sess_nope/feed", json={"data": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"


def test_validation_error_does_not_echo_input(client) -> None:
    r = client.post(
        "/match",
        json={"data": 12345, "patterns": [{"id": "p", "data": "a"}]},
    )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    # 输入值 12345 不应出现在响应里。
    assert "12345" not in r.text


def test_offset_conflict_is_undetermined_and_recoverable(client) -> None:
    v = create_version(client, [{"id": "a", "data": "a"}])
    sid = client.post("/sessions", json={"version_id": v["version_id"]}).json()["sid"]
    client.post(f"/sessions/{sid}/feed", json={"data": "aa"})

    r = client.post(
        f"/sessions/{sid}/feed", json={"data": "a", "expected_offset": 99}
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "OFFSET_MISMATCH"
    assert err["outcome"] == "undetermined"
    assert err["details"]["server_offset"] == 2

    # 用正确偏移重试即恢复。
    r2 = client.post(
        f"/sessions/{sid}/feed", json={"data": "a", "expected_offset": 2}
    )
    assert r2.status_code == 200 and r2.json()["new_hits"] == 1


def test_diagnostics_record_reasons_and_redact(client) -> None:
    v = create_version(client, [{"id": "a", "data": "a"}])
    sid = client.post(
        "/sessions",
        json={"version_id": v["version_id"]},
        headers={"X-Request-ID": "diag-req-1"},
    ).json()["sid"]
    # 触发一次接受。
    client.post(f"/sessions/{sid}/feed", json={"data": "aaa"})
    # 触发一次拒绝。
    client.post(f"/sessions/{sid}/feed", json={"data": "a", "expected_offset": 50})

    r = client.get(f"/diagnostics?sid={sid}")
    assert r.status_code == 200
    events = r.json()["items"]
    outcomes = {e["code"]: e for e in events}
    assert "CHUNK_ACCEPTED" in outcomes
    assert "OFFSET_MISMATCH" in outcomes
    accepted = outcomes["CHUNK_ACCEPTED"]
    assert accepted["outcome"] == "accept"
    assert accepted["key_state"]["new_hits"] == 3
    # 脱敏：原始字节不以明文出现，只有长度信息。
    assert accepted["key_state"]["chunk"] == {"type": "bytes", "len": 3}
    # 请求标识贯通。
    mismatch = outcomes["OFFSET_MISMATCH"]
    assert mismatch["key_state"]["details"]["server_offset"] == 3

    # 按 request_id 过滤可用。
    r2 = client.get("/diagnostics?request_id=diag-req-1")
    assert r2.json()["total"] >= 1
