"""场景三方对照测试：手写期望 × 独立 oracle × HTTP 服务（Parquet+SQLite）。

断言粒度：行身份集合、逐行 KEPT/DELETED/FILTERED 处置、删除理由 (kind,seq)、
存活文件集合、时间旅行读取值、错误提交的类别与错误码。
"""
from __future__ import annotations

from fixtures.replay import ReplayFailure, replay_scenario


def test_scenario_three_way_cross_check(client, scenario):
    result = replay_scenario(client, scenario)
    # 至少发生过一次提交
    ok = [v for v in result.versions if v.status == "OK"]
    assert ok, f"{scenario.name}: no successful version"


def test_every_service_row_has_driver_identity(client, scenario):
    result = replay_scenario(client, scenario)
    last = [v for v in result.versions if v.status == "OK"][-1]
    rows = client.get(f"/tables/{result.table_id}/rows").json()
    assert len(rows["rows"]) == len(rows["row_drivers"])
    for d in rows["row_drivers"]:
        assert d["position"] >= 0 and d["file_id"]
    # 行号均落在其所属文件元数据行数内
    files = client.get(f"/tables/{result.table_id}/files").json()["live_files"]
    counts = {f["file_id"]: f["row_count"] for f in files}
    for d in rows["row_drivers"]:
        assert d["position"] < counts[d["file_id"]]


def test_replay_failures_carry_category():
    # 元测试：ReplayFailure 类别字段必须存在（失败可分类是硬性要求）
    try:
        raise ReplayFailure("DISPOSITION_MISMATCH", "x")
    except ReplayFailure as exc:
        assert exc.category == "DISPOSITION_MISMATCH"
