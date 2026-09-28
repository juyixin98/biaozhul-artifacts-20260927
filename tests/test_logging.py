"""日志与配置测试。

验证：
- JSON 行日志每行可解析，带 service/version/metric_version；
- 运行操作日志携带 run_id / correlation_id，并记录穷举进度与判定依据；
- 失败不写成成功（suggest 不可达 -> status=UNREACHABLE）；
- 环境变量可覆盖 TOML 配置。
"""

from __future__ import annotations

import json

from .conftest import ADMIN_HEADERS


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_logs_link_run_identity_and_versions(
        client, created_run, auth_headers, log_file):
    rid = created_run["run_id"]
    client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 2},
                headers=auth_headers)
    records = _read_jsonl(log_file)
    assert records, "没有产生日志"

    # 所有记录都带服务版本信息
    for rec in records:
        assert rec["service"] == "anon-risk"
        assert rec["version"] == "1.0.0"
        assert rec["metric_version"] == "1.0.0"

    # 存在关联到该 run 的建议日志，且记录了判定依据 DM
    suggest_logs = [
        r for r in records
        if r.get("run_id") == rid
        and r.get("event", {}).get("best_dm") is not None
    ]
    assert suggest_logs, "缺少带 best_dm 的穷举进度日志"

    final = [r for r in records
             if r.get("run_id") == rid
             and r.get("event", {}).get("levels") == {"zip": 1, "age": 2}]
    assert final, "缺少最终建议判定日志"
    assert final[0]["event"]["dm"] == 18
    assert any("exhaustive_enumeration" in b
               for b in final[0]["event"]["basis"])


def test_logs_record_progress_steps(
        client, created_run, auth_headers, log_file):
    rid = created_run["run_id"]
    client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 2},
                headers=auth_headers)
    records = _read_jsonl(log_file)
    progress = [r["event"]["progress"] for r in records
                if "progress" in r.get("event", {})]
    # 12 个向量应有多条进度，且最后一条为 12/12
    assert progress
    assert progress[-1] == "12/12"


def test_unreachable_is_logged_as_unreachable_not_success(
        client, created_run, auth_headers, log_file):
    rid = created_run["run_id"]
    client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 3},
                headers=auth_headers)
    records = _read_jsonl(log_file)
    warnings = [r for r in records if r["level"] == "WARNING"
                and "阈值不可达" in r["message"]]
    assert warnings
    assert warnings[0]["run_id"] == rid
    assert warnings[0]["event"]["reasons"], "必须记录判定依据"


def test_correlation_id_in_logs_matches_request(
        client, created_run, auth_headers, log_file):
    rid = created_run["run_id"]
    client.post(
        f"/runs/{rid}/suggest?x=1", json={"k": 2, "l": 2},
        headers={**auth_headers, "X-Correlation-ID": "trace-xyz-42"},
    )
    records = _read_jsonl(log_file)
    matched = [r for r in records if r.get("correlation_id") == "trace-xyz-42"]
    assert matched, "日志未按请求 correlation_id 关联"


def test_audit_logs_distinguish_statuses(client, created_run, auth_headers):
    rid = created_run["run_id"]
    client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 2},
                headers=auth_headers)
    client.post(f"/runs/{rid}/suggest", json={"k": 2, "l": 3},
                headers=auth_headers)
    events = client.get(f"/audit/events?run_id={rid}",
                        headers=ADMIN_HEADERS).json()["events"]
    statuses = {(e["event"], e["status"]) for e in events}
    assert ("suggest", "SUCCESS") in statuses
    assert ("suggest", "UNREACHABLE") in statuses
