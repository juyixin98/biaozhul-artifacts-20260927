"""Parquet 格式适配：Arrow 表的读写、内容指纹与行计数。

内容身份（content identity）使用 SHA-256：对"按列名排序后逐列的原始
记录值"计算哈希，与文件字节解耦——同数据重写（压缩参数不同等）
仍得到相同指纹；任何一行/一列变化都会改变指纹。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from ..errors import ComputeFailureError, InputError

# 本合成服务支持的 Arrow 类型白名单（类型名 -> pa 类型）
SUPPORTED_TYPES: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "int32": pa.int32(),
    "string": pa.string(),
    "bool": pa.bool_(),
    "float64": pa.float64(),
}


def resolve_type(type_name: str) -> pa.DataType:
    try:
        return SUPPORTED_TYPES[type_name]
    except KeyError:
        raise InputError(
            f"不支持的列类型 {type_name!r}",
            supported=sorted(SUPPORTED_TYPES),
        ) from None


def write_parquet(table: pa.Table, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="snappy")
    tmp.replace(path)


def read_parquet(path: str | Path) -> pa.Table:
    try:
        return pq.read_table(path)
    except Exception as exc:  # 底层文件损坏/非 parquet
        raise ComputeFailureError(f"Parquet 文件无法读取: {path}", cause=str(exc)) from exc


def row_count(path: str | Path) -> int:
    return pq.read_metadata(str(path)).num_rows


def table_to_pylist(table: pa.Table) -> list[dict[str, Any]]:
    """转成行字典列表；统一 None 表示 NULL。"""
    return table.to_pylist()


def pylist_to_table(rows: list[dict[str, Any]], columns: dict[str, str]) -> pa.Table:
    """按声明 schema 把行字典构造成 Arrow 表，做类型强制与 NULL 处理。"""
    names = list(columns)
    arrays: dict[str, pa.Array] = {}
    for name in names:
        type_name = columns[name]
        vals = [r.get(name) for r in rows]
        try:
            arr = _build_array(vals, resolve_type(type_name), name)
        except InputError:
            raise
        except Exception as exc:
            raise ComputeFailureError(
                f"列 {name!r} 构造 Arrow 数组失败", cause=str(exc),
            ) from exc
        arrays[name] = arr
    return pa.table({name: arrays[name] for name in names})


def _build_array(vals: list[Any], arrow_type: pa.DataType, col: str) -> pa.Array:
    t = str(arrow_type)
    out: list[Any] = []
    for v in vals:
        if v is None:
            out.append(None)
            continue
        if t in ("int64", "int32"):
            if isinstance(v, bool) or not isinstance(v, int):
                raise InputError(f"列 {col!r} 期望整数，收到 {v!r} ({type(v).__name__})")
            if t == "int32" and not (-(2**31) <= v <= 2**31 - 1):
                raise InputError(f"列 {col!r} 的值 {v} 超出 int32 范围")
            out.append(v)
        elif t == "float64":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise InputError(f"列 {col!r} 期望数值，收到 {v!r}")
            out.append(float(v))
        elif t == "bool":
            if not isinstance(v, bool):
                raise InputError(f"列 {col!r} 期望布尔值，收到 {v!r}")
            out.append(v)
        elif t == "string":
            if not isinstance(v, str):
                raise InputError(f"列 {col!r} 期望字符串，收到 {v!r}")
            out.append(v)
    return pa.array(out, type=arrow_type)


def content_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    """对行数据做稳定哈希（与 parquet 字节无关）。"""
    h = hashlib.sha256()
    for row in rows:
        h.update(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode())
        h.update(b"\n")
    return h.hexdigest()
