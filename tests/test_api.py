"""端到端 API 测试: 注册 -> 审计 -> 剪枝查询 -> 诊断/脱敏。"""
import pytest
from fastapi.testclient import TestClient

from colaudit.api import create_app
from colaudit.config import Settings
from oracle import load_ground_truth, oracle_predicate


@pytest.fixture
def client(tmp_path, fixture_root):
    settings = Settings(
        home=tmp_path / "home",
        fixtures_dir=fixture_root,
        mask_sensitive=True,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, fixture_root


def _register(c, root, name):
    resp = c.post("/datasets/register", json={"name": name,
                                              "root": str(root / name)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health_and_request_id(client):
    c, _ = client
    resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"]
    assert resp.json()["request_id"] == resp.headers["X-Request-ID"]


def test_audit_and_query_well_formed(client):
    c, root = client
    _register(c, root, "well_formed")
    r = c.post("/audit", json={"dataset": "well_formed"}).json()
    assert r["summary"]["all_trusted"] is True

    q = c.post("/query", json={
        "dataset": "well_formed", "column": "id",
        "op": "gt", "value": 119,
    }).json()
    truth = load_ground_truth(root / "well_formed")
    expected = [x["id"] for x in oracle_predicate(truth, "id", "gt", 119)]
    assert [row["id"] for row in q["rows"]] == expected
    assert q["correctness"]["matches_baseline"] is True
    assert q["pages_skipped"] >= 1  # 受信统计确实剪了枝


def test_bad_statistics_query_remains_correct(client):
    c, root = client
    _register(c, root, "bad_statistics")
    audit = c.post("/audit", json={
        "dataset": "bad_statistics",
        "mask_sensitive": False,
    }).json()
    assert audit["summary"]["all_trusted"] is False

    for op, val in [("gt", 5.0), ("lt", 100.0), ("is_null", None)]:
        body = {"dataset": "bad_statistics", "column": "score", "op": op}
        if val is not None:
            body["value"] = val
        q = c.post("/query", json=body).json()
        truth = load_ground_truth(root / "bad_statistics")
        expected = [x["id"] for x in oracle_predicate(truth, "score", op, val)]
        assert [row["id"] for row in q["rows"]] == expected, (op, val)
        assert q["correctness"]["matches_baseline"] is True, op

    # 坏页轨迹必须显式标注未受信
    q = c.post("/query", json={
        "dataset": "bad_statistics", "column": "score", "op": "gt",
        "value": 5.0,
    }).json()
    untrusted = [t for t in q["page_trace"]
                 if t["action"] == "scanned_untrusted"]
    assert {
        (t["file"], t["row_group"], t["page"]) for t in untrusted
    } >= {("data.parquet", 0, 0), ("data.parquet", 0, 1)}


def test_no_statistics_requires_scan(client):
    c, root = client
    _register(c, root, "no_statistics")
    c.post("/audit", json={"dataset": "no_statistics"})
    q = c.post("/query", json={
        "dataset": "no_statistics", "column": "score",
        "op": "gt", "value": 1000.0,
    }).json()
    assert q["matched_rows"] == 0
    assert q["pages_skipped"] == 0  # 无统计 -> 不剪枝
    assert q["pages_scanned"] == 6
    assert q["correctness"]["matches_baseline"] is True


def test_sensitive_column_redacted(client):
    c, root = client
    _register(c, root, "sensitive_demo")
    c.post("/audit", json={"dataset": "sensitive_demo"})
    diag = c.get("/datasets/sensitive_demo/diagnostics").json()
    # 诊断事件中 name 列的 min/max 必须打码
    name_events = [
        e for e in diag["diagnostics"] if e["column_name"] == "name"
        and e["scope"] == "page"
    ]
    assert name_events
    for e in name_events:
        assert e["state"]["claimed"]["min"] in (None, "[REDACTED]")
        assert e["state"]["claimed"]["max"] in (None, "[REDACTED]")

    q = c.post("/query", json={
        "dataset": "sensitive_demo", "column": "id", "op": "gt", "value": -1,
    }).json()
    assert all(row["name"] == "[REDACTED]" for row in q["rows"] if row["name"])


def test_query_before_audit_conflicts(client):
    c, root = client
    _register(c, root, "well_formed")
    resp = c.post("/query", json={
        "dataset": "well_formed", "column": "score", "op": "gt",
        "value": 1.0,
    })
    assert resp.status_code == 409


def test_audit_report_persisted_and_fetchable(client):
    c, root = client
    _register(c, root, "bad_statistics")
    r = c.post("/audit", json={"dataset": "bad_statistics"}).json()
    fetched = c.get(f"/audit/{r['run_id']}").json()
    codes = {e["code"] for e in fetched["diagnostics"]}
    assert "minmax_mismatch" in codes
    assert "null_count_mismatch" in codes
    assert "sorted_mismatch" in codes
    assert all(e.get("request_id") for e in fetched["diagnostics"])


def test_register_unknown_dataset_404(client):
    c, _ = client
    resp = c.post("/audit", json={"dataset": "missing"})
    assert resp.status_code == 404
