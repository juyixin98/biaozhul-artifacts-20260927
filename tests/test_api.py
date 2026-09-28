"""HTTP 端到端测试：错误契约（code/category/status）、流式头、run_id 诊断。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.api import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 用环境变量让 lifespan 在临时 DB 上构建唯一的 Repository/Service，
    # 避免手动注入与 lifespan 各持一份连接导致读写不一致。
    monkeypatch.setenv("NRP_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _upload(client, text):
    r = client.post("/sources", json={"text": text})
    assert r.status_code == 201, r.text
    return r.json()


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_plan_and_get_detail_and_apply_stream(client, record):
    src = _upload(client, "alice@example.com bob@test.org")
    payload = {
        "source_id": src["source_id"],
        "rules": [
            {
                "rule_id": "mail",
                "pattern": r"(?P<u>\w+)@(?P<h>\w+)\.(\w+)",
                "template": r"<\g<u>|host=\g<h>|tld=\3>",
                "priority": 10,
            }
        ],
    }
    r = client.post("/plans", json=payload)
    assert r.status_code == 201, r.text
    run_id = r.headers["x-run-id"]
    body = r.json()
    record.state("plan_summary", body)
    assert body["replacement_count"] == 2
    assert body["source_spec"]["sha256"] == src["spec"]["sha256"]

    d = client.get(f"/plans/{body['plan_id']}").json()
    first = d["replacements"][0]
    record.state("first_replacement", first)
    assert first["matched"] == "alice@example.com"
    assert first["replacement"] == "<alice|host=example|tld=com>"
    # 捕获组结构完整（含组名与字符/字节范围）
    names = [g["name"] for g in first["groups"]]
    assert names == ["u", "h", None]
    assert first["byte_start"] == 0

    ar = client.post(f"/plans/{body['plan_id']}/apply")
    assert ar.status_code == 200, ar.text
    assert ar.headers["x-replaced-count"] == "2"
    assert ar.headers["x-run-id"]
    record.state("applied_text", ar.text)
    assert ar.text == "<alice|host=example|tld=com> <bob|host=test|tld=org>"

    diag = client.get(f"/diagnostics/{run_id}").json()
    events = [(e["stage"], e["event"]) for e in diag["events"]]
    record.state("diag", events)
    assert ("plan", "plan_built") in events


def test_collect_apply_returns_new_version(client):
    src = _upload(client, "xx")
    r = client.post(
        "/plans",
        json={"source_id": src["source_id"], "rules": [
            {"rule_id": "x", "pattern": "x", "template": "Y"}
        ]},
    )
    plan_id = r.json()["plan_id"]
    ar = client.post(f"/plans/{plan_id}/apply/collect")
    assert ar.status_code == 200, ar.text
    out = ar.json()
    assert out["output_version"] == 2
    assert out["replaced"] == 2


def test_regex_unsupported_feature_error_contract(client, record):
    src = _upload(client, "abc")
    r = client.post(
        "/plans",
        json={"source_id": src["source_id"], "rules": [
            {"rule_id": "backref", "pattern": r"(\w)\1", "template": r"\1"}
        ]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    record.fail_category(err["code"], err["category"], r.status_code)
    assert err["code"] == "COMPUTE_REGEX_COMPILE"
    assert err["category"] == "compute"
    assert "reason" in err["details"]


def test_invalid_template_error_contract(client):
    src = _upload(client, "abc")
    r = client.post(
        "/plans",
        json={"source_id": src["source_id"], "rules": [
            {"rule_id": "t", "pattern": "(a)", "template": r"\9"}
        ]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INPUT_INVALID_TEMPLATE"
    assert err["category"] == "input"
    assert err["details"]["group_count"] == 1


def test_version_mismatch_over_http(client, record):
    src = _upload(client, "hello world")
    plan = client.post(
        "/plans",
        json={"source_id": src["source_id"], "rules": [
            {"rule_id": "w", "pattern": r"\w+", "template": "W"}
        ]},
    ).json()
    # 改写源产生新版本
    upd = client.put(f"/sources/{src['source_id']}", json={"text": "changed content entirely"})
    assert upd.status_code == 200
    ar = client.post(f"/plans/{plan['plan_id']}/apply/collect")
    assert ar.status_code == 409
    err = ar.json()["error"]
    record.fail_category(err["code"], err["category"], ar.status_code)
    assert err["code"] == "STATE_SOURCE_VERSION_MISMATCH"
    assert err["category"] == "state"
    assert err["details"]["current_version"] == 2
    assert err["details"]["plan_bound_version"] == 1


def test_double_apply_conflict_over_http(client):
    src = _upload(client, "abc")
    plan = client.post(
        "/plans",
        json={"source_id": src["source_id"], "rules": [
            {"rule_id": "a", "pattern": "a", "template": "X"}
        ]},
    ).json()["plan_id"]
    assert client.post(f"/plans/{plan}/apply/collect").status_code == 200
    r = client.post(f"/plans/{plan}/apply/collect")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STATE_PLAN_ALREADY_APPLIED"


def test_duplicate_rule_id_rejected(client):
    src = _upload(client, "ab")
    r = client.post(
        "/plans",
        json={"source_id": src["source_id"], "rules": [
            {"rule_id": "r", "pattern": "a", "template": "1"},
            {"rule_id": "r", "pattern": "b", "template": "2"},
        ]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INPUT_INVALID_RULE"


def test_unknown_source_404_category(client):
    r = client.post(
        "/plans",
        json={"source_id": "src-missing", "rules": [
            {"rule_id": "a", "pattern": "a", "template": "1"}
        ]},
    )
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["category"] == "state"
    assert err["code"] == "STATE_SOURCE_NOT_FOUND"


def test_rules_validate_reports_each(client):
    r = client.post(
        "/rules/validate",
        json=[
            {"rule_id": "ok", "pattern": r"(?P<a>\d+)", "template": r"\g<a>"},
            {"rule_id": "bad", "pattern": r"(x)\1", "template": r"\1"},
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body[0]["ok"] is True and body[0]["group_names"] == ["a"]
    assert body[1]["ok"] is False and "COMPUTE_REGEX_COMPILE" in body[1]["reason"]


def test_multibyte_plan_over_http(client):
    src = _upload(client, "你好world世界")
    r = client.post(
        "/plans",
        json={"source_id": src["source_id"], "rules": [
            {"rule_id": "han", "pattern": r"你好|世界", "template": "HH"}
        ]},
    )
    plan = r.json()
    assert plan["replacement_count"] == 2
    ar = client.post(f"/plans/{plan['plan_id']}/apply")
    assert ar.text == "HHworldHH"
