"""Parquet 读写与内容指纹。所有 pyarrow 依赖集中在本模块，内核不直接 import pyarrow。"""
from __future__ import annotations

import datetime as _dt
import hashlib
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from app.contracts.types import normalize_value
from app.errors import ComputationFailed

_PA_TYPES = {
    "string": pa.string(),
    "long": pa.int64(),
    "int": pa.int32(),
    "double": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
}

POSITION_FILE_SCHEMA = pa.schema([pa.field("target_file_id", pa.string()), pa.field("position", pa.int64())])


def pa_type(logical_type: str) -> pa.DataType:
    return _PA_TYPES[logical_type]


def build_table_schema(columns: list[dict[str, Any]]) -> pa.Schema:
    return pa.schema([pa.field(c["name"], _PA_TYPES[c["type"]]) for c in columns])


def _to_physical(value: Any, logical_type: str, *, column: str) -> Any:
    value = normalize_value(value, logical_type, column=column)
    if value is None:
        return None
    if logical_type == "date":
        return _dt.date.fromisoformat(value)
    return value


def normalize_row(
    row: dict[str, Any], columns: list[dict[str, Any]]
) -> dict[str, Any]:
    """按表 schema 规范化一行：缺列补 NULL，多余列拒绝；逐值做严格类型转换。"""
    unknown = sorted(set(row) - {c["name"] for c in columns})
    if unknown:
        from app.errors import ValidationError

        raise ValidationError("UNKNOWN_COLUMN", "row contains unknown columns", {"columns": unknown})
    return {
        c["name"]: _to_physical(row.get(c["name"]), c["type"], column=c["name"]) for c in columns
    }


def write_data_file(path: str | Path, rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> str:
    """写数据 Parquet，返回内容 SHA-256。

    rows 必须是已由 contracts.types.normalize_value 规范化的“物理值”行
    （date 为 datetime.date）；提交器经 planner 完成规范化后调用本函数。
    """
    schema = build_table_schema(columns)
    data = {c["name"]: [_to_physical_checked(r.get(c["name"]), c["type"], c["name"])
                        for r in rows] for c in columns}
    return _write_parquet(path, pa.Table.from_pydict(data, schema=schema))


def _to_physical_checked(value: Any, logical_type: str, column: str) -> Any:
    """规范化后的逻辑值 -> 物理值（date 字符串转 date）；非法形态仍按输入错误拒绝。"""
    if value is None:
        return None
    if logical_type == "date":
        if isinstance(value, _dt.date):
            return value
        if isinstance(value, str):
            try:
                return _dt.date.fromisoformat(value)
            except ValueError:
                from app.errors import ValidationError

                raise ValidationError(
                    "TYPE_MISMATCH", f"column '{column}': invalid ISO date", {"column": column}
                )
        from app.errors import ValidationError

        raise ValidationError("TYPE_MISMATCH", f"column '{column}': expected ISO date", {"column": column})
    if logical_type in ("int", "long") and isinstance(value, bool):
        from app.errors import ValidationError

        raise ValidationError(
            "TYPE_MISMATCH", f"column '{column}': bool is not an integer", {"column": column}
        )
    return value


def write_position_delete_file(path: str | Path, target_file_id: str, positions: Iterable[int]) -> str:
    positions = list(positions)
    data = {"target_file_id": [target_file_id] * len(positions), "position": positions}
    return _write_parquet(path, pa.Table.from_pydict(data, schema=POSITION_FILE_SCHEMA))


def write_equality_delete_file(
    path: str | Path, key_columns: list[str], columns: list[dict[str, Any]], predicates: list[dict[str, Any]]
) -> str:
    """写等值删除文件。predicates 来自 planner，键值已规范化（允许显式 None）。"""
    type_by_name = {c["name"]: c["type"] for c in columns}
    key_schema = pa.schema([pa.field(k, _PA_TYPES[type_by_name[k]]) for k in key_columns])
    data: dict[str, list[Any]] = {k: [] for k in key_columns}
    for pred in predicates:
        key = pred["key"]
        for k in key_columns:
            data[k].append(_to_physical_checked(key[k], type_by_name[k], k))
    return _write_parquet(path, pa.Table.from_pydict(data, schema=key_schema))


def _write_parquet(path: str | Path, table: pa.Table) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换，避免半截文件落盘
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="snappy")
    tmp.replace(path)
    return sha256_file(path)


def read_parquet(path: str | Path, columns: list[str] | None = None) -> list[dict[str, Any]]:
    """读取为 Python 行；date32 统一回转 ISO 字符串，使接口层只见逻辑值。"""
    try:
        table = pq.read_table(path, columns=columns)
    except Exception as exc:  # 损坏文件等 -> COMPUTATION_FAILED
        raise ComputationFailed(
            "PARQUET_READ_FAILED", f"failed to read parquet file {path}: {exc}", {"path": str(path)}
        )
    rows = table.to_pylist()
    return [_logical_row(r) for r in rows]


def _logical_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if isinstance(v, (_dt.date, _dt.datetime)) else v
    return out


def parquet_row_count(path: str | Path) -> int:
    try:
        return pq.read_metadata(path).num_rows
    except Exception as exc:
        raise ComputationFailed(
            "PARQUET_METADATA_FAILED", f"failed to read parquet metadata: {exc}", {"path": str(path)}
        )


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()
