"""HTTP 接口端到端测试：请求身份、错误语义、作业状态机、原始报文双臂结果。"""

from __future__ import annotations

import base64
import time

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.media.rtp import build_rtp


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "jobs.db"), log_json=False)
    with TestClient(app) as c:
        yield c
    app.state.runner.stop()


def _simple_packets(n: int = 20, ratio: float = 1.0):
    out = []
    gap = int(20_000 * ratio)
    for i in range(n):
        raw = build_rtp(sequence=i, timestamp=i * 160, ssrc=7,
                        payload=b"\x01\x02" * 80)
        out.append({
            "arrival_us": 1_000_000 + i * gap,
            "rtp_base64": base64.b64encode(raw).decode(),
        })
    return out


def test_health_reports_version_and_scenarios(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert "burst_reorder" in body["scenarios"]


def test_request_id_is_echoed_and_propagated(client) -> None:
    rid = "test-request-1234"
    r = client.get("/health", headers={"X-Request-ID": rid})
    assert r.headers["X-Request-ID"] == rid
    assert r.headers["X-Service-Version"]


def test_scenario_sync_pass_with_assertion_rows(client) -> None:
    r = client.post("/validate/scenario", json={"scenario": "wraparound"})
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    ids = {a["id"] for a in body["assertions"]}
    assert {"oracle.adaptive", "oracle.fixed", "wrap.events"} <= ids


def test_unknown_scenario_is_404_with_stable_code(client) -> None:
    r = client.post("/validate/scenario", json={"scenario": "bogus"})
    assert r.status_code == 404
    assert r.json()["error_code"] == "unknown_scenario"
    assert r.json()["details"]["available"]


def test_raw_plan_adaptive_and_fixed_arms(client) -> None:
    r = client.post("/validate/plan", json={"packets": _simple_packets()})
    assert r.status_code == 200
    body = r.json()
    assert body["adaptive"]["totals"]["frames"] == 20
    assert body["fixed"]["totals"]["frames"] == 20
    assert body["oracle_passed"] is True
    # 失败原因与不确定单列
    assert "parse_errors" in body and "uncertain" in body


def test_malformed_packet_reports_parse_error_not_500(client) -> None:
    bad = base64.b64encode(b"\x80\x60").decode()
    r = client.post("/validate/plan",
                    json={"packets": [{"arrival_us": 1, "rtp_base64": bad}]})
    # 不抛 500：解析失败被归类为 parse_error 丢弃
    assert r.status_code == 200
    errs = r.json()["parse_errors"]
    assert errs and errs[0]["reason"] == "parse_error"
    assert "truncated" in errs[0]["detail"]


def test_async_job_lifecycle_and_persistence(client) -> None:
    r = client.post("/jobs/scenario", json={"scenario": "clock_drift"})
    assert r.status_code == 202
    jid = r.json()["job_id"]
    assert r.json()["location"] == f"/jobs/{jid}"

    final = None
    for _ in range(100):
        row = client.get(f"/jobs/{jid}").json()
        if row["status"] in ("succeeded", "failed"):
            final = row
            break
        time.sleep(0.02)
    assert final is not None and final["status"] == "succeeded"
    assert final["result"]["passed"] is True
    assert final["result"]["version"]
    # 作业列表可见
    listed = client.get("/jobs").json()["jobs"]
    assert any(j["job_id"] == jid for j in listed)


def test_get_missing_job_404(client) -> None:
    r = client.get("/jobs/job_does_not_exist")
    assert r.status_code == 404
    assert r.json()["error_code"] == "not_found"
