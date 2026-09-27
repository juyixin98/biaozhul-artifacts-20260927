"""诊断模块小测试：请求 ID 与 trace 字段。"""
from __future__ import annotations

import json

from app.diagnostics.tracer import RequestTracer, new_request_id


def test_request_id_unique_and_hex():
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(x) == 16 for x in ids)


def test_tracer_finish_shape():
    t = RequestTracer(request_id="abc")
    t.expression = "a AND b"
    t.version = 1
    t.status = "ok"
    t.result_count = 3
    t.stats = {"blocks_skipped": 2}
    t.steps = [{"op": "intersect"}]
    payload = t.finish()
    # JSON 可序列化（要写进 SQLite/JSONL）
    json.dumps(payload, ensure_ascii=False)
    assert payload["request_id"] == "abc"
    assert payload["finished_at"] >= payload["started_at"]
