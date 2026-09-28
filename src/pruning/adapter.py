"""格式适配层（format adapter）。

用 **PyArrow** 真正生成并读取 Parquet 文件，从 Arrow 元数据中提取
min/max/null_count/row_count，不允许硬编码演示统计。

目录布局（Hive 风格）::

    <root>/<table>/<partcol>=<bucket>/part-xxxx.parquet

另支持两类合成场景用于验证保守性：
* ``missing_stats``：某列统计标记为缺失（present=False）；
* ``truncated_strings``：字符串 min/max 被截断（truncated=True）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from . import transforms as T
from .model import ColumnStats, FileEntry, PartitionEntry, TableMetadata

_PART_RE = re.compile(r"^([^=]+)=([^=]+)$")


def logical_type(arrow_type: pa.DataType) -> str:
    if pa.types.is_timestamp(arrow_type) or pa.types.is_date(arrow_type):
        return "timestamp"
    if pa.types.is_integer(arrow_type):
        return "int64"
    if pa.types.is_floating(arrow_type):
        return "double"
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return "string"
    if pa.types.is_boolean(arrow_type):
        return "bool"
    return "unknown"


def write_parquet_file(path: str, columns: dict[str, list], types: dict[str, pa.DataType]):
    """用给定 Arrow 类型写 Parquet。时间戳列以 UTC epoch 秒 int64 存储（逻辑类型 timestamp）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arrays = {}
    for name, values in columns.items():
        at = types[name]
        if pa.types.is_timestamp(at):
            # 输入为 UTC epoch 秒（可负）。先按单位 "s" 解释再转 us，
            # 直接 int64->timestamp[us] 会把秒误当微秒。
            secs = pa.array(values, type=pa.int64())
            arrays[name] = pc.cast(
                pc.cast(secs, pa.timestamp("s", tz="UTC")),
                pa.timestamp("us", tz="UTC"))
        else:
            arrays[name] = pa.array(values, type=at)
    table = pa.table(arrays)
    pq.write_table(table, path, compression="snappy",
                   write_statistics=True, use_dictionary=False)
    return table


@dataclass
class ReadOptions:
    # 需要按截断方式处理的字符串列（模拟统计读取器只保留前缀）
    truncated_string_columns: tuple[str, ...] = ()
    truncate_prefix_len: int = 4
    # 需要当作"统计缺失"处理的列
    missing_stat_columns: tuple[str, ...] = ()
    # 物理数据层是否真正截断该列（仅用于生成夹具，验证读取器无法越界裁剪）
    physically_truncate_columns: tuple[str, ...] = ()


def read_file_stats(path: str, file_id: str, part_value: str,
                    partition_column: str, opts: ReadOptions | None = None) -> FileEntry:
    """读取一个 Parquet 文件，逐行组汇总统计 -> FileEntry。"""
    opts = opts or ReadOptions()
    pf = pq.ParquetFile(path)
    meta = pf.metadata
    schema = pf.schema_arrow
    row_count = meta.num_rows

    stats: dict[str, ColumnStats] = {}
    for ci, field in enumerate(schema):
        name = field.name
        ltype = logical_type(field.type)
        if name in opts.missing_stat_columns:
            stats[name] = ColumnStats(column=name, type=ltype, present=False,
                                      row_count=row_count,
                                      truncation_note="统计缺失（读取器不可得）")
            continue

        mn = mx = None
        null_count = 0
        have_min = have_max = False
        for rg in range(meta.num_row_groups):
            st = meta.row_group(rg).column(ci).statistics
            if st is None:
                continue
            if st.has_null_count:
                null_count += st.null_count
            if st.has_min_max:
                rmin, rmax = st.min, st.max
                # Arrow 对 timestamp[us,tz=UTC] 统计返回 datetime（UTC）
                if ltype == "timestamp":
                    rmin = _dt_to_epoch(rmin)
                    rmax = _dt_to_epoch(rmax)
                if not have_min or rmin < mn:
                    mn, have_min = rmin, True
                if not have_max or rmax > mx:
                    mx, have_max = rmax, True

        truncated = False
        note = ""
        if ltype == "string" and name in opts.truncated_string_columns and \
                have_min and have_max:
            # 模拟读取器只保留前缀：真实极值可能更长 -> 标记可能截断
            mn = mn[:opts.truncate_prefix_len] if isinstance(mn, str) else mn
            mx = mx[:opts.truncate_prefix_len] if isinstance(mx, str) else mx
            truncated = True
            note = f"min/max 仅保留前 {opts.truncate_prefix_len} 字符，真实极值未知"

        stats[name] = ColumnStats(
            column=name, type=ltype,
            min_value=mn if have_min else None,
            max_value=mx if have_max else None,
            null_count=null_count, row_count=row_count, present=True,
            truncated=truncated, truncation_note=note)

    return FileEntry(file_id=file_id, physical_path=path, partition_value=part_value,
                     row_count=row_count, stats=stats,
                     size_bytes=os.path.getsize(path))


def _dt_to_epoch(value):
    """UTC datetime -> epoch 秒（dateconv 固定规则）。"""
    import datetime as _dt
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return int(value.timestamp())
    if isinstance(value, _dt.date):
        return int(_dt.datetime(value.year, value.month, value.day,
                                tzinfo=_dt.timezone.utc).timestamp())
    return value


def discover_table(root: str, table: str, partition_column: str,
                   opts: ReadOptions | None = None) -> TableMetadata:
    """扫描目录，发现分区与 Parquet 文件，读取真实统计。"""
    opts = opts or ReadOptions()
    table_dir = os.path.join(root, table)
    partitions: dict[str, PartitionEntry] = {}
    columns: dict[str, str] = {}
    if not os.path.isdir(table_dir):
        raise FileNotFoundError(table_dir)

    for dirpath, _dirnames, filenames in os.walk(table_dir):
        base = os.path.basename(dirpath)
        m = _PART_RE.match(base)
        part_value = m.group(2) if m and m.group(1) == partition_column else None
        for fn in sorted(filenames):
            if not fn.endswith(".parquet"):
                continue
            path = os.path.join(dirpath, fn)
            fid = os.path.relpath(path, root)
            entry = read_file_stats(path, fid, part_value or "", partition_column, opts)
            for cname, cs in entry.stats.items():
                columns.setdefault(cname, cs.type)
            pv = part_value or "__NO_PART__"
            partitions.setdefault(
                pv, PartitionEntry(column=partition_column, value=part_value or ""))
            partitions[pv].files.append(entry)

    return TableMetadata(
        table=table, partition_column=partition_column,
        transform=T.transform_identity(),
        partitions=sorted(partitions.values(), key=lambda p: p.value),
        columns=columns)
