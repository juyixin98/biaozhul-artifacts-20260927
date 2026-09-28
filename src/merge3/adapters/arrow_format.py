"""格式适配层：PyArrow Schema/Parquet 读写、规范化、内容寻址。

执行内核只消费"规范化行"（dict[str, JSON 标量]），不知道 Parquet 的存在；
本层负责把 JSON 行与不可变 Parquet 文件互转，并保证：
- 快照内容哈希只取决于 schema + 规范化后的数据（行序按主键排序，列序固定）；
- 同一内容永远产生同一哈希（去重存储、可核验）；
- 类型不匹配或缺列显式报错，不静默吞掉。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from ..domain.models import TableSpec
from ..errors import ValidationError

# 支持的列类型。保持小而明确，避免隐式类型猜测导致的"同值不等"。
_TYPE_MAP: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "float64": pa.float64(),
    "string": pa.string(),
    "bool": pa.bool_(),
    "date32": pa.date32(),
    "timestamp_us": pa.timestamp("us"),
}

_CANONICAL_PY_TYPES = {
    "int64": int,
    "float64": float,
    "string": str,
    "bool": bool,
}


def build_arrow_schema(spec: TableSpec) -> pa.Schema:
    if not spec.primary_key:
        raise ValidationError(f"table {spec.name}: primary_key 不能为空")
    names = [f.name for f in spec.fields]
    if len(names) != len(set(names)):
        raise ValidationError(f"table {spec.name}: 列名重复 {names}")
    missing = [k for k in spec.primary_key if k not in names]
    if missing:
        raise ValidationError(f"table {spec.name}: 主键列未在字段中声明: {missing}")
    try:
        fields = [pa.field(f.name, _TYPE_MAP[f.type], nullable=f.nullable) for f in spec.fields]
    except KeyError as e:  # pragma: no cover - 防御
        raise ValidationError(f"table {spec.name}: 不支持的列类型 {e}") from None
    return pa.schema(fields)


def _coerce_scalar(fname: str, ftype: str, value: Any) -> Any:
    if value is None:
        return None
    if ftype in _CANONICAL_PY_TYPES:
        want = _CANONICAL_PY_TYPES[ftype]
        # bool 是 int 的子类：接受 "true"/"false" 字符串之外不做隐式转换。
        if isinstance(value, bool) and want is int:
            raise ValidationError(f"列 {fname}: 期望 int64，实际 bool")
        if not isinstance(value, want):
            # 允许 int -> float 的数值列宽化，其余严格。
            if ftype == "float64" and isinstance(value, int) and not isinstance(value, bool):
                return float(value)
            raise ValidationError(
                f"列 {fname}: 期望 {ftype}，实际 {type(value).__name__}={value!r}"
            )
        return value
    if ftype in ("date32", "timestamp_us"):
        if not isinstance(value, str):
            raise ValidationError(f"列 {fname}: {ftype} 需要 ISO 字符串，实际 {type(value).__name__}")
        return value  # pa 直接解析 ISO 字符串
    raise ValidationError(f"列 {fname}: 未知类型 {ftype}")  # pragma: no cover


def rows_to_table(
    rows: Iterable[dict[str, Any]], spec: TableSpec
) -> tuple[pa.Table, int]:
    """校验并构造 PyArrow 表；主键缺失/重复、列缺失、类型不符都显式报错。

    返回 (table, row_count)。
    """
    schema = build_arrow_schema(spec)
    type_by_name = {f.name: f.type for f in spec.fields}
    cols: dict[str, list[Any]] = {f.name: [] for f in spec.fields}
    seen: set[tuple[Any, ...]] = set()
    count = 0
    for i, row in enumerate(rows):
        extra = set(row) - set(cols)
        if extra:
            raise ValidationError(f"第 {i} 行存在未声明列: {sorted(extra)}")
        key_parts: list[Any] = []
        for fname in cols:
            if fname not in row:
                raise ValidationError(f"第 {i} 行缺少列 {fname!r}")
            val = _coerce_scalar(fname, type_by_name[fname], row[fname])
            cols[fname].append(val)
            if fname in spec.primary_key:
                if val is None:
                    raise ValidationError(f"第 {i} 行主键列 {fname} 为 null")
                key_parts.append(val)
        key = tuple(key_parts)
        if key in seen:
            raise ValidationError(f"第 {i} 行主键重复: {_json_key(key_parts)}")
        seen.add(key)
        count += 1
    return pa.Table.from_pydict(cols, schema=schema), count  # type: ignore[return-value]


def table_to_rows(table: pa.Table, spec: TableSpec) -> list[dict[str, Any]]:
    """读取为规范化 JSON 行（date/timestamp 转 ISO 字符串）。"""
    out: list[dict[str, Any]] = []
    for batch in table.to_batches():
        d = batch.to_pydict()
        for i in range(batch.num_rows):
            row: dict[str, Any] = {}
            for f in spec.fields:
                v = d[f.name][i]
                row[f.name] = _scalar_to_canonical(v)
            out.append(row)
    return out


def _scalar_to_canonical(v: Any) -> Any:
    if v is None:
        return None
    # pyarrow date32 / timestamp 从 pydict 返回 datetime.date / datetime.datetime
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def canonical_content(spec: TableSpec, rows: list[dict[str, Any]]) -> tuple[str, str, int]:
    """返回 (canonical_json, sha256, row_count)。

    行按主键值排序、列按声明顺序输出，确保哈希与输入顺序无关、与文件无关。
    """
    names = [f.name for f in spec.fields]

    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(_sortable(row[k]) for k in spec.primary_key)

    ordered = sorted(rows, key=sort_key)
    payload = {
        "table": spec.name,
        "primary_key": list(spec.primary_key),
        "fields": [
            {"name": f.name, "type": f.type, "nullable": f.nullable} for f in spec.fields
        ],
        "rows": [[row[n] for n in names] for row in ordered],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return blob, hashlib.sha256(blob.encode("utf-8")).hexdigest(), len(ordered)


def write_parquet(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 压缩开启但不影响内容哈希；snappy 是 pyarrow 内置编解码。
    pq.write_table(table, path, compression="snappy", version="2.6")


def read_parquet(path: Path) -> pa.Table:
    return pq.read_table(path)


def _sortable(v: Any) -> Any:
    # 混合类型排序时（如异常数据）保持确定性；当前主键仅 int/string/bool。
    return (type(v).__name__, v)


def _json_key(parts: list[Any]) -> str:
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
