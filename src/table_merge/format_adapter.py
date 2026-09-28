"""格式适配层：JSON 行集 <-> PyArrow Table <-> Parquet 文件。

职责：
* 严格类型校验（拒绝未知列、缺列、bool/int 混淆、NaN/Inf）；
* 主键非空与唯一性校验；
* 确定性写出（写入前按主键排序、固定压缩与行组），
  使相同逻辑数据产生相同字节，配合内容哈希做幂等去重。
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .errors import SnapshotFormatError
from .models import Column, TableSchema, key_string, key_tuple

_PA_TYPES: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "float64": pa.float64(),
    "string": pa.string(),
    "bool": pa.bool_(),
}


def _check_scalar(value: Any, col_type: str, table: str, column: str) -> Any:
    if value is None:
        return None
    if col_type == "int64":
        # 明确拒绝 bool 与带小数的 float，避免 Python 里 True == 1 的隐式坑
        if isinstance(value, bool) or not isinstance(value, int):
            raise SnapshotFormatError(
                f"column {table}.{column} expects int64, got {type(value).__name__}",
                details={"column": column, "value": value},
            )
    elif col_type == "float64":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SnapshotFormatError(
                f"column {table}.{column} expects float64, got {type(value).__name__}",
                details={"column": column, "value": value},
            )
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            raise SnapshotFormatError(
                f"column {table}.{column} does not accept NaN/Infinity",
                details={"column": column},
            )
    elif col_type == "string":
        if not isinstance(value, str):
            raise SnapshotFormatError(
                f"column {table}.{column} expects string, got {type(value).__name__}",
                details={"column": column, "value": value},
            )
    elif col_type == "bool":
        if not isinstance(value, bool):
            raise SnapshotFormatError(
                f"column {table}.{column} expects bool, got {type(value).__name__}",
                details={"column": column, "value": value},
            )
    return value


def validate_rows(rows: list[dict[str, Any]], schema: TableSchema) -> list[dict[str, Any]]:
    """校验并返回规范化后的行（深拷贝、列顺序固定、按主键排序）。"""
    schema.validate()
    expected = schema.column_names()
    seen: set[tuple] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise SnapshotFormatError(f"row #{index} is not an object", details={"index": index})
        extra = set(raw) - set(expected)
        missing = set(expected) - set(raw)
        if extra:
            raise SnapshotFormatError(
                f"row #{index} has unknown columns: {sorted(extra)}",
                details={"index": index, "extra": sorted(extra)},
            )
        if missing:
            raise SnapshotFormatError(
                f"row #{index} misses columns: {sorted(missing)}",
                details={"index": index, "missing": sorted(missing)},
            )
        row = {}
        for col in schema.columns:
            row[col.name] = _check_scalar(raw[col.name], col.type, schema.table, col.name)
        key = key_tuple(row, schema.primary_key)
        if any(part is None for part in key):
            raise SnapshotFormatError(
                f"row #{index} has null primary key {key_string(key)!r}",
                details={"index": index, "key": list(key)},
            )
        if key in seen:
            raise SnapshotFormatError(
                f"duplicate primary key {key_string(key)!r} at row #{index}",
                details={"index": index, "key": list(key)},
            )
        seen.add(key)
        normalized.append(row)
    normalized.sort(key=lambda r: key_tuple(r, schema.primary_key))
    return normalized


def rows_to_arrow(rows: list[dict[str, Any]], schema: TableSchema) -> pa.Table:
    normalized = validate_rows(rows, schema)
    arrays: dict[str, pa.Array] = {}
    for col in schema.columns:
        values = [row[col.name] for row in normalized]
        arrays[col.name] = pa.array(values, type=_PA_TYPES[col.type])
    return pa.Table.from_pydict(arrays, schema=pa.schema(
        [pa.field(col.name, _PA_TYPES[col.type], nullable=True) for col in schema.columns]
    ))


def write_parquet(rows: list[dict[str, Any]], schema: TableSchema, path: str | Path) -> str:
    """确定性写出 Parquet，返回文件 sha256。"""
    table = rows_to_arrow(rows, schema)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 单行组 + 固定压缩 + 不写额外版本元数据，尽量保证字节级确定性
    pq.write_table(
        table,
        path,
        compression="snappy",
        use_dictionary=False,
        write_statistics=False,
        row_group_size=max(table.num_rows, 1),
    )
    return sha256_file(path)


def sha256_file(path: str | Path, buf_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(buf_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_parquet(path: str | Path) -> list[dict[str, Any]]:
    """读取 Parquet 为 Python 行（列顺序取文件 schema，值均为 Python 原生标量）。"""
    table = pq.read_table(path)
    return table.to_pylist()


def arrow_schema_to_table_schema(table_name: str, arrow_schema: pa.Schema,
                                 primary_key: tuple[str, ...]) -> TableSchema:
    reverse = {pa.int64(): "int64", pa.float64(): "float64",
               pa.string(): "string", pa.bool_(): "bool"}
    cols: list[dict] = []
    for field in arrow_schema:
        logical = reverse.get(field.type)
        if logical is None:
            raise SnapshotFormatError(
                f"unsupported parquet type {field.type} for column {field.name}",
                details={"column": field.name, "type": str(field.type)},
            )
        cols.append({"name": field.name, "type": logical})
    schema = TableSchema(
        table=table_name,
        columns=tuple(Column(c["name"], c["type"]) for c in cols),
        primary_key=primary_key,
    )
    schema.validate()
    return schema
