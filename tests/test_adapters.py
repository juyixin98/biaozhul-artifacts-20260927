"""格式适配层测试：Parquet 往返、date 逻辑类型、删除文件结构、内容指纹稳定性。"""
from __future__ import annotations

import pyarrow.parquet as pq

from app.adapters import pyarrow_ops as p

COLUMNS = [
    {"name": "id", "type": "long"},
    {"name": "name", "type": "string"},
    {"name": "d", "type": "date"},
    {"name": "flag", "type": "boolean"},
]


def test_data_file_roundtrip_and_hash(tmp_path):
    rows = [
        {"id": 1, "name": "a", "d": "2026-01-01", "flag": True},
        {"id": 2, "name": None, "d": None, "flag": False},
    ]
    path = tmp_path / "data.parquet"
    # planner 先规范化；这里直接构造规范化后的物理值行
    norm = [p.normalize_row(r, COLUMNS) for r in rows]
    h1 = p.write_data_file(path, norm, COLUMNS)
    h2 = p.sha256_file(path)
    assert h1 == h2 and len(h1) == 64
    back = p.read_parquet(path)
    assert back[0] == {"id": 1, "name": "a", "d": "2026-01-01", "flag": True}
    assert back[1]["d"] is None and back[1]["name"] is None
    assert p.parquet_row_count(path) == 2


def test_position_delete_file_schema(tmp_path):
    path = tmp_path / "pd.parquet"
    p.write_position_delete_file(path, "file-abc", [3, 7])
    table = pq.read_table(path)
    assert table.schema.names == ["target_file_id", "position"]
    assert p.read_parquet(path) == [
        {"target_file_id": "file-abc", "position": 3},
        {"target_file_id": "file-abc", "position": 7},
    ]


def test_equality_delete_file_keeps_null_keys(tmp_path):
    path = tmp_path / "ed.parquet"
    p.write_equality_delete_file(
        path, ["id"], COLUMNS,
        [{"key": {"id": 5}}, {"key": {"id": None}}],
    )
    back = p.read_parquet(path)
    assert back[0]["id"] == 5 and back[1]["id"] is None


def test_hash_changes_with_content(tmp_path):
    rows1 = [p.normalize_row({"id": 1, "name": "a", "d": "2026-01-01", "flag": True}, COLUMNS)]
    rows2 = [p.normalize_row({"id": 1, "name": "b", "d": "2026-01-01", "flag": True}, COLUMNS)]
    h1 = p.write_data_file(tmp_path / "a.parquet", rows1, COLUMNS)
    h2 = p.write_data_file(tmp_path / "b.parquet", rows2, COLUMNS)
    assert h1 != h2
