"""格式适配层：把外部源数据（JSON records / NDJSON / Parquet）适配成统一的 SourceRow 流。

适配层只负责：
  1. 读入并解析（解析失败 = SOURCE_FORMAT_ERROR，带格式/行号定位）；
  2. 标量值校验与列收集（非标量 = SOURCE_FORMAT_ERROR）；
  3. 规范化稀疏行（缺列补 NULL）与稳定字节估算（供资源限制与快照指纹使用）。

适配层 *不* 做任何键唯一性/匹配判断——那些是 planner 基于操作前快照的决策。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import SourceRow, jsonable
from .errors import SourceFormatError

_SCALAR_TYPES = (bool, int, float, str, bytes)


@dataclass(frozen=True)
class SourceBatch:
    format: str
    columns: tuple[str, ...]
    rows: tuple[SourceRow, ...]
    total_bytes: int


def load_source(raw: Any, *, key_columns: tuple[str, ...]) -> SourceBatch:
    """统一入口。raw 形态：

    {"format": "records", "records": [{...}, ...]}
    {"format": "ndjson", "content": "line\\nline\\n"} 或 {"path": "/abs/..."}
    {"format": "parquet", "path": "/abs/..."}
    """
    if not isinstance(raw, dict):
        raise SourceFormatError("source must be an object with a 'format' field")
    fmt = raw.get("format")
    if fmt == "records":
        records, total_bytes = _from_records(raw)
    elif fmt == "ndjson":
        records, total_bytes = _from_ndjson(raw)
    elif fmt == "parquet":
        records, total_bytes = _from_parquet(raw)
    else:
        raise SourceFormatError(f"unsupported source format: {fmt!r}",
                                details={"supported": ["records", "ndjson", "parquet"]})

    rows, columns = _normalize(records, key_columns)
    return SourceBatch(format=fmt, columns=columns, rows=tuple(rows), total_bytes=total_bytes)


# ---- 三种输入 ---------------------------------------------------------------

def _from_records(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    records = raw.get("records")
    if not isinstance(records, list):
        raise SourceFormatError("records source requires a list field 'records'")
    total = 0
    for i, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            raise SourceFormatError(f"record #{i} is not an object",
                                    details={"rownum": i})
        total += canonical_size(rec)
    return records, total


def _from_ndjson(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    text = _read_text(raw)
    records: list[dict[str, Any]] = []
    total = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SourceFormatError(
                f"invalid JSON on NDJSON line {lineno}: {exc.msg}",
                details={"line": lineno, "column": exc.colno},
            ) from exc
        if not isinstance(rec, dict):
            raise SourceFormatError(f"NDJSON line {lineno} is not an object",
                                    details={"line": lineno})
        records.append(rec)
        total += len(line.encode("utf-8"))
    return records, total


def _from_parquet(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    path = _require_path(raw, "parquet")
    try:
        table = pq.read_table(str(path))
    except (OSError, pa.ArrowException) as exc:
        raise SourceFormatError(f"cannot read parquet file: {exc}",
                                details={"path": str(path)}) from exc
    try:
        records = table.to_pylist()
    except (pa.ArrowException, Exception) as exc:  # noqa: BLE001 - 转换失败统一归类
        raise SourceFormatError(f"cannot convert parquet rows: {exc}",
                                details={"path": str(path)}) from exc
    total = sum(canonical_size(r) for r in records)
    return records, total


def _read_text(raw: dict[str, Any]) -> str:
    if "content" in raw:
        if not isinstance(raw["content"], str):
            raise SourceFormatError("ndjson 'content' must be a string")
        return raw["content"]
    path = _require_path(raw, "ndjson")
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceFormatError(f"cannot read ndjson file: {exc}",
                                details={"path": str(path)}) from exc


def _require_path(raw: dict[str, Any], fmt: str) -> Path:
    p = raw.get("path")
    if not isinstance(p, str):
        raise SourceFormatError(f"{fmt} source requires a string 'path'",
                                details={"fields": sorted(raw.keys())})
    path = Path(p)
    if not path.is_absolute():
        raise SourceFormatError("source path must be absolute",
                                details={"path": p})
    if not path.exists():
        raise SourceFormatError("source path does not exist", details={"path": p})
    return path


# ---- 规范化 -----------------------------------------------------------------

def _normalize(
    records: Iterable[dict[str, Any]],
    key_columns: tuple[str, ...],
) -> tuple[list[SourceRow], tuple[str, ...]]:
    rows: list[SourceRow] = []
    columns: set[str] = set(key_columns)
    for i, rec in enumerate(records, start=1):
        values: dict[str, Any] = {}
        for name, value in rec.items():
            if not isinstance(name, str):
                raise SourceFormatError(f"record #{i} has a non-string column name",
                                        details={"rownum": i})
            values[name] = _check_scalar(value, i, name)
            columns.add(name)
        rows.append(SourceRow(rownum=i, values=values))

    # 键列恒定存在；其余列按名字排序得到稳定列序
    payload = sorted(columns - set(key_columns))
    ordered_cols = tuple(key_columns) + tuple(payload)
    # 稀疏行缺列补 NULL（不回填键列：键列缺失等价于键值 NULL，由 planner 依策略裁决）
    for row in rows:
        for col in payload:
            row.values.setdefault(col, None)
    return rows, ordered_cols


def _check_scalar(value: Any, rownum: int, column: str) -> Any:
    if value is None or isinstance(value, _SCALAR_TYPES):
        return value
    raise SourceFormatError(
        f"non-scalar value at row {rownum} column {column!r}",
        details={"rownum": rownum, "column": column, "type": type(value).__name__},
    )


# ---- 稳定估算 ---------------------------------------------------------------

def canonical_size(obj: Any) -> int:
    """规范化 JSON 的 UTF-8 字节数；bytes 以 base64 计入。

    排序键，使同一逻辑数据的估算与行序/插入序无关。
    """
    payload = json.dumps(jsonable(obj), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return len(payload.encode("utf-8"))
