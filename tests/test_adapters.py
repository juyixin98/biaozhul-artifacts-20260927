"""格式适配：inline / inbox JSON / inbox Parquet，指纹与类型校验。"""
from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

from deleter.adapters import parquet_format as pf
from deleter.adapters.ingestion import IngestionService
from deleter.errors import InputError, ResourceLimitError


def test_inbox_json_roundtrip(tmp_path):
    inbox = tmp_path / "inbox"
    svc = IngestionService(inbox)
    (inbox / "data.json").write_text(json.dumps([{"id": 1}]), encoding="utf-8")
    rows = svc.load_rows({"kind": "inbox", "name": "data.json"})
    assert rows == [{"id": 1}]


def test_inbox_parquet_roundtrip(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    t = pa.table({"id": pa.array([1, 2], type=pa.int64()),
                  "name": pa.array(["a", "b"])})
    pq.write_table(t, inbox / "d.parquet")
    svc = IngestionService(inbox)
    rows = svc.load_rows({"kind": "inbox", "name": "d.parquet"})
    assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_fingerprint_stable_and_distinct():
    a = [{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]
    b = [{"name": "x", "id": 1}, {"id": 2, "name": "y"}]  # 列顺序不同
    c = [{"id": 1, "name": "x"}, {"id": 2, "name": "z"}]
    assert pf.content_fingerprint(a) == pf.content_fingerprint(b)
    assert pf.content_fingerprint(a) != pf.content_fingerprint(c)


def test_bool_not_coerced_to_int(tmp_path):
    svc = IngestionService(tmp_path)
    with __import__("pytest").raises(InputError):
        svc.validate_against_schema([{"id": True}], {"id": "int64"})


def test_row_quota(tmp_path):
    svc = IngestionService(tmp_path, max_rows_per_load=2)
    try:
        svc.load_rows({"kind": "inline", "rows": [{"id": 1}, {"id": 2}, {"id": 3}]})
        assert False, "应当抛资源限制"
    except ResourceLimitError as e:
        assert e.details["actual"] == 3
