"""格式适配层：JSON 记录 <-> PyArrow Table -> 本地不可变 Parquet 文件。

职责边界：
- 只负责模式校验、记录转换、Parquet 原子写入与读回校验、sha256 指纹。
- 不接触快照/事务语义；不关心文件最终挂到哪个快照。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from lake_txn import errors

# 支持的逻辑类型 -> PyArrow 类型
SUPPORTED_TYPES: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "float64": pa.float64(),
    "bool": pa.bool_(),
    "string": pa.string(),
}


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str


@dataclass(frozen=True)
class WrittenFile:
    path: Path
    sha256: str
    size_bytes: int
    row_count: int


def build_schema(columns: Sequence[ColumnSpec]) -> pa.Schema:
    fields: list[pa.Field] = []
    for col in columns:
        if col.type not in SUPPORTED_TYPES:
            raise errors.unprocessable(
                errors.UNSUPPORTED_TYPE,
                f"不支持的列类型: {col.name}={col.type}",
                {"column": col.name, "type": col.type, "allowed": sorted(SUPPORTED_TYPES)},
            )
        fields.append(pa.field(col.name, SUPPORTED_TYPES[col.type]))
    return pa.schema(fields)


def coerce_value(value: Any, logical_type: str, column: str) -> Any:
    """严格转换单值；bool 不接受 "true" 之类的字符串，避免隐式歧义。"""
    if value is None:
        return None
    try:
        if logical_type == "int64":
            if isinstance(value, bool):
                raise ValueError("bool 不可作为 int64")
            return int(value)
        if logical_type == "float64":
            if isinstance(value, bool):
                raise ValueError("bool 不可作为 float64")
            return float(value)
        if logical_type == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError("bool 列只接受 true/false")
        if logical_type == "string":
            if isinstance(value, str):
                return value
            raise ValueError("string 列只接受字符串")
    except (TypeError, ValueError) as exc:
        raise errors.bad_request(
            errors.VALIDATION_ERROR,
            f"列 {column} 的值无法转换为 {logical_type}",
            {"column": column, "expected": logical_type},
        ) from exc
    raise errors.unprocessable(errors.UNSUPPORTED_TYPE, f"未知类型 {logical_type}", {"column": column})


def records_to_table(
    records: Iterable[dict[str, Any]], columns: Sequence[ColumnSpec]
) -> pa.Table:
    schema = build_schema(columns)
    by_name = {c.name: c.type for c in columns}
    coerced: list[dict[str, Any]] = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise errors.bad_request(
                errors.VALIDATION_ERROR,
                f"第 {i} 行不是 JSON 对象",
                {"row_index": i},
            )
        extra = set(rec) - set(by_name)
        if extra:
            raise errors.bad_request(
                errors.VALIDATION_ERROR,
                f"第 {i} 行存在模式外字段: {sorted(extra)}",
                {"row_index": i, "extra": sorted(extra)},
            )
        coerced.append(
            {c.name: coerce_value(rec.get(c.name), c.type, c.name) for c in columns}
        )
    arrays = {
        c.name: pa.array([row[c.name] for row in coerced], type=SUPPORTED_TYPES[c.type])
        for c in columns
    }
    return pa.Table.from_pydict(arrays, schema=schema)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_parquet_atomic(
    records: Iterable[dict[str, Any]],
    columns: Sequence[ColumnSpec],
    dest_dir: Path,
    filename: str,
) -> WrittenFile:
    """先写临时文件、flush+fsync，再原子 rename 为最终文件名。

    最终文件名由调用方给定（内容寻址），因此“写到一半被看到”不可能发生。
    """
    table = records_to_table(records, columns)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final_path = dest_dir / filename
    fd, tmp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=dest_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            pq.write_table(table, fh, compression="snappy")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    digest = sha256_file(final_path)
    return WrittenFile(
        path=final_path,
        sha256=digest,
        size_bytes=final_path.stat().st_size,
        row_count=table.num_rows,
    )


def verify_parquet(
    path: Path,
    columns: Sequence[ColumnSpec],
    expected_sha256: str,
    expected_row_count: int | None = None,
) -> tuple[str, int]:
    """读回校验：文件可解析、模式兼容、sha256 一致、行数一致。

    返回 (sha256, 行数)。模式检查按列名+类型精确匹配（不做隐式演进）。
    """
    if not path.is_file():
        raise errors.bad_request(
            errors.FILE_NOT_STAGED,
            f"暂存文件不存在: {path.name}",
            {"file": path.name},
        )
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha256:
        raise errors.unprocessable(
            errors.FILE_HASH_MISMATCH,
            f"文件内容指纹与提交声明不一致: {path.name}",
            {"file": path.name, "expected_sha256": expected_sha256, "actual_sha256": actual_sha},
        )
    schema = pq.ParquetFile(path).schema_arrow
    expected = build_schema(columns)
    if not schema.equals(expected, check_metadata=False):
        raise errors.unprocessable(
            errors.SCHEMA_MISMATCH,
            f"文件 Parquet 模式与表模式不匹配: {path.name}",
            {
                "file": path.name,
                "file_schema": str(schema),
                "table_schema": str(expected),
            },
        )
    row_count = pq.ParquetFile(path).metadata.num_rows
    if expected_row_count is not None and row_count != expected_row_count:
        raise errors.unprocessable(
            errors.STAGE_VALIDATION_FAILED,
            f"文件行数与声明不一致: {path.name}",
            {"file": path.name, "expected_rows": expected_row_count, "actual_rows": row_count},
        )
    return actual_sha, row_count


def read_rows(path: Path) -> list[dict[str, Any]]:
    """把 Parquet 文件读成 Python dict 行（供验证/工具使用）。"""
    return pq.read_table(path).to_pylist()
