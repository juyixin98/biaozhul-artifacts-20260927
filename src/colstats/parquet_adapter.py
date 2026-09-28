"""Parquet 格式适配层。

职责：
1. 解析 Parquet 文件的页脚（FileMetaData）与每个数据页页头，提取页级
   与列块级的 min/max/NULL 统计（:class:`FileModel`）；
2. 通过 PyArrow 读取 *实际数据*，供执行内核独立重算真值（不信任文件
   自己的统计）；
3. 提供改写能力（:func:`rewrite_file`），用于构造"数据正确但统计错误"
   的本地夹具 —— 改页头/页脚统计、排序声明，重建文件。

页级真值切分：对于 max_repetition_level=0 的扁平列，数据页的
num_values 就是值槽位数（含 NULL），按顺序切片即可逐页重算。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

from . import parquet_codec as tc
from .models import (
    ChunkClaim,
    Claim,
    ColumnSchema,
    FileModel,
    PageClaim,
    RowGroupClaim,
    SortingClaim,
)

PARQUET_MAGIC = b"PAR1"

# PageHeader / DataPageHeader / Statistics 字段号（parquet.thrift）
_PH_TYPE = 1
_PH_UNCOMPRESSED = 2
_PH_COMPRESSED = 3
_PH_CRC = 4
_PH_DATA_PAGE_V1 = 5
_PH_DATA_PAGE_V2 = 8
_PH_DICTIONARY = 7

_DP1_NUM_VALUES = 1
_DP1_STATS = 5
_DP2_NUM_VALUES = 1
_DP2_NUM_NULLS = 2
_DP2_NUM_ROWS = 3
_DP2_STATS = 8

# Statistics
_ST_MAX = 1
_ST_MIN = 2
_ST_NULL_COUNT = 3
_ST_MAX_TRUNC = 7
_ST_MIN_TRUNC = 8
_ST_MAX_V2 = 5
_ST_MIN_V2 = 6

# ColumnMetaData
_CMD_TYPE = 1
_CMD_ENCODINGS = 2
_CMD_PATH_IN_SCHEMA = 3
_CMD_NUM_VALUES = 5
_CMD_TOTAL_UNCOMPRESSED = 7
_CMD_TOTAL_COMPRESSED = 6
_CMD_DATA_OFFSET = 9
_CMD_DICT_OFFSET = 10
_CMD_STATS = 12
_CMD_KEY_VALUE = 13

# RowGroup
_RG_COLUMNS = 1
_RG_TOTAL_SIZE = 2
_RG_NUM_ROWS = 3
_RG_SORTING = 4

# FileMetaData
_FM_VERSION = 1
_FM_SCHEMA = 2
_FM_NUM_ROWS = 3
_FM_ROW_GROUPS = 4

# SchemaElement
_SE_TYPE = 1
_SE_NAME = 4
_SE_NUM_CHILDREN = 3
_SE_REPETITION = 5

# SortingColumn
_SC_COLUMN = 1
_SC_DESC = 2
_SC_NULLS_FIRST = 3

_PT_BOOLEAN = 0
_PT_INT32 = 1
_PT_INT64 = 2
_PT_FLOAT = 4
_PT_DOUBLE = 5
_PT_BYTE_ARRAY = 6

_PHYSICAL_TYPES = {
    0: "BOOLEAN",
    1: "INT32",
    2: "INT64",
    3: "INT96",
    4: "FLOAT",
    5: "DOUBLE",
    6: "BYTE_ARRAY",
    7: "FIXED_LEN_BYTE_ARRAY",
}


class ParquetFormatError(ValueError):
    pass


# ---------------------------------------------------------------- 解码


def _decode_stat_bytes(raw: bytes, physical_type: str) -> Any:
    """按物理类型解码 statistics 里的 PLAIN 编码值。

    BYTE_ARRAY 的统计直接是原始字节（没有长度前缀，见 parquet 规范）。
    """
    if physical_type == "BOOLEAN":
        return raw != b"\x00"
    if physical_type == "INT32" or physical_type == "FLOAT":
        fmt = "<i" if physical_type == "INT32" else "<f"
        return struct.unpack(fmt, raw)[0]
    if physical_type == "INT64" or physical_type == "DOUBLE":
        fmt = "<q" if physical_type == "INT64" else "<d"
        return struct.unpack(fmt, raw)[0]
    if physical_type == "BYTE_ARRAY":
        return raw
    if physical_type == "FIXED_LEN_BYTE_ARRAY":
        return raw
    raise ParquetFormatError(f"不支持解码统计的物理类型: {physical_type}")


def encode_stat_bytes(value: Any, physical_type: str) -> bytes:
    """统计值编码回 PLAIN 字节（BYTE_ARRAY 无长度前缀）。"""
    if physical_type == "BOOLEAN":
        return b"\x01" if value else b"\x00"
    if physical_type == "INT32":
        return struct.pack("<i", int(value))
    if physical_type == "INT64":
        return struct.pack("<q", int(value))
    if physical_type == "FLOAT":
        return struct.pack("<f", float(value))
    if physical_type == "DOUBLE":
        return struct.pack("<d", float(value))
    if physical_type in ("BYTE_ARRAY", "FIXED_LEN_BYTE_ARRAY"):
        return bytes(value)
    raise ParquetFormatError(f"不支持编码统计的物理类型: {physical_type}")


def _claim_from_stats(stats_nodes: list[tc.TNode] | None, physical_type: str) -> Claim:
    if stats_nodes is None:
        return Claim(has_min_max=False, has_null_count=False)
    st = tc.TNode(None, stats_nodes)
    has_mm = False
    minv = maxv = None

    def _exact(fid: int, using_v2: bool) -> bool:
        # fid 7/8 是 is_max/min_value_exact：为 true 才表示精确。
        # 旧字段 min/max（1/2）按规范恒为精确；v2 字段存在但标志
        # 缺失/为 false 时，保守地视为被截断（禁止边界剪枝）。
        node = st.get(fid)
        if node is not None:
            return bool(node.value)
        return not using_v2

    min_node_v2 = st.get(_ST_MIN_V2)
    max_node_v2 = st.get(_ST_MAX_V2)
    min_node = min_node_v2 or st.get(_ST_MIN)
    max_node = max_node_v2 or st.get(_ST_MAX)
    min_trunc = not _exact(_ST_MIN_TRUNC, min_node_v2 is not None)
    max_trunc = not _exact(_ST_MAX_TRUNC, max_node_v2 is not None)
    # 旧字段 min/max（1/2）或新字段 min_value/max_value（6/5）
    min_node = st.get(_ST_MIN_V2) or st.get(_ST_MIN)
    max_node = st.get(_ST_MAX_V2) or st.get(_ST_MAX)
    if min_node is not None and max_node is not None:
        has_mm = True
        minv = _decode_stat_bytes(min_node.value, physical_type)
        maxv = _decode_stat_bytes(max_node.value, physical_type)
    nc_node = st.get(_ST_NULL_COUNT)
    null_count = nc_node.value if nc_node else None
    return Claim(
        has_min_max=has_mm,
        null_count=null_count,
        has_null_count=nc_node is not None,
        min_claim=minv,
        max_claim=maxv,
        min_truncated=min_trunc,
        max_truncated=max_trunc,
    )


def _logical_type_name(schema_arrow_field: pa.Field) -> str | None:
    t = schema_arrow_field.type
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "STRING"
    if pa.types.is_decimal(t):
        return "DECIMAL"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP"
    return None


def _parse_schema(footer: tc.TNode, pf: pq.ParquetFile) -> list[ColumnSchema]:
    arrow_schema = pf.schema_arrow
    result: list[ColumnSchema] = []
    # schema 节点列表：第 0 个是 root，其后是叶子（扁平 schema 情形）
    elements = footer.require(_FM_SCHEMA).structs()
    leaf_index = 0
    for i, el in enumerate(elements[1:], start=1):
        node = tc.TNode(None, el)
        type_node = node.get(_SE_TYPE)
        if type_node is None:
            # 嵌套组节点：跳过（本实现只审计扁平叶子）
            continue
        physical = _PHYSICAL_TYPES.get(type_node.value)
        rep = node.get(_SE_REPETITION)
        max_rep = 1 if rep is not None and rep.value == 2 else 0
        max_def = 1 if rep is not None and rep.value in (0, 1) else 0
        arrow_field = arrow_schema.field(leaf_index)
        result.append(
            ColumnSchema(
                index=leaf_index,
                path=node.require(_SE_NAME).value.decode("utf-8"),
                physical_type=physical or f"UNKNOWN({type_node.value})",
                logical_type=_logical_type_name(arrow_field),
                converted_type=None,
                max_repetition=max_rep,
                max_definition=max_def,
            )
        )
        leaf_index += 1
    return result


def parse_file(path: str | Path) -> FileModel:
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) < 12 or raw[:4] != PARQUET_MAGIC or raw[-4:] != PARQUET_MAGIC:
        raise ParquetFormatError(f"{path}: 不是合法 Parquet 文件（魔数错误）")
    footer_len = struct.unpack("<I", raw[-8:-4])[0]
    footer_bytes = raw[-8 - footer_len : -8]
    footer_nodes, consumed = tc.decode_struct(footer_bytes)
    if consumed != footer_len:
        raise ParquetFormatError(
            f"{path}: 页脚解析长度不一致 {consumed}!={footer_len}"
        )
    footer = tc.TNode(None, footer_nodes)
    pf = pq.ParquetFile(path)

    schema = _parse_schema(footer, pf)
    schema_by_path = {c.path: c for c in schema}

    row_groups: list[RowGroupClaim] = []
    for rg_idx, rg_nodes in enumerate(footer.require(_FM_ROW_GROUPS).structs()):
        rg = tc.TNode(None, rg_nodes)
        rg_model = RowGroupClaim(
            row_group_index=rg_idx, num_rows=rg.require(_RG_NUM_ROWS).value
        )
        sorting_nodes = rg.get(_RG_SORTING)
        if sorting_nodes is not None:
            for sc_nodes in sorting_nodes.structs():
                sc = tc.TNode(None, sc_nodes)
                rg_model.sorting.append(
                    SortingClaim(
                        column_idx=sc.require(_SC_COLUMN).value,
                        descending=bool(sc.get(_SC_DESC) and sc.get(_SC_DESC).value),
                        nulls_first=bool(
                            sc.get(_SC_NULLS_FIRST) and sc.get(_SC_NULLS_FIRST).value
                        ),
                    )
                )
        for ci, chunk_nodes in enumerate(rg.require(_RG_COLUMNS).structs()):
            chunk = tc.TNode(None, chunk_nodes)
            meta = tc.TNode(None, chunk.get(3).value)  # ColumnChunk.meta_data
            physical = _PHYSICAL_TYPES.get(meta.require(_CMD_TYPE).value, "UNKNOWN")
            path_vec = meta.require(_CMD_PATH_IN_SCHEMA).scalars()
            col_path = b".".join(path_vec).decode("utf-8")
            data_offset = meta.require(_CMD_DATA_OFFSET).value
            tcs = meta.require(_CMD_TOTAL_COMPRESSED).value
            num_values = meta.require(_CMD_NUM_VALUES).value
            chunk_claim = _claim_from_stats(
                meta.get(_CMD_STATS).value if meta.get(_CMD_STATS) else None,
                physical,
            )
            cm = ChunkClaim(
                column_index=ci,
                path=col_path,
                physical_type=physical,
                logical_type=(
                    schema_by_path[col_path].logical_type
                    if col_path in schema_by_path
                    else None
                ),
                converted_type=None,
                data_offset=data_offset,
                total_compressed_size=tcs,
                num_values=num_values,
                claim=chunk_claim,
            )
            cm.pages = _parse_pages(raw, data_offset, tcs, physical)
            rg_model.chunks.append(cm)
        row_groups.append(rg_model)

    return FileModel(
        path=str(path),
        size=len(raw),
        num_rows=footer.require(_FM_NUM_ROWS).value,
        schema=schema,
        row_groups=row_groups,
        raw=raw,
        footer_nodes=footer_nodes,
    )


def _parse_pages(
    raw: bytes, offset: int, total_compressed: int, physical_type: str
) -> list[PageClaim]:
    # 数据页的物理结尾 = header_end + compressed_page_size，若页头带
    # CRC（fid 4）则再追加 4 字节。
    end = offset + total_compressed
    entries: list[dict] = []
    pos = offset
    while pos < end:
        header_pos = pos
        nodes, header_end = tc.decode_struct(raw, pos)
        ph = tc.TNode(None, nodes)
        ptype = ph.require(_PH_TYPE).value
        csize = ph.require(_PH_COMPRESSED).value
        has_crc = ph.get(_PH_CRC) is not None
        claim = Claim(has_min_max=False, has_null_count=False)
        num_values = 0
        dp = ph.get(_PH_DATA_PAGE_V1)
        dp2 = ph.get(_PH_DATA_PAGE_V2)
        if dp is not None:
            dp1 = tc.TNode(None, dp.value)
            num_values = dp1.require(_DP1_NUM_VALUES).value
            stats = dp1.get(_DP1_STATS)
            claim = _claim_from_stats(
                stats.value if stats is not None else None, physical_type
            )
        elif dp2 is not None:
            d2 = tc.TNode(None, dp2.value)
            num_values = d2.require(_DP2_NUM_VALUES).value
            stats = d2.get(_DP2_STATS)
            claim = _claim_from_stats(
                stats.value if stats is not None else None, physical_type
            )
        entries.append(
            {
                "ptype": ptype,
                "header_pos": header_pos,
                "header_end": header_end,
                "num_values": num_values,
                "claim": claim,
                "nodes": nodes,
                "csize": csize,
                "has_crc": has_crc,
            }
        )
        pos = header_end + csize + (4 if has_crc else 0)

    pages: list[PageClaim] = []
    page_index = 0
    for entry in entries:
        if entry["ptype"] not in (0, 5):
            continue
        claim = entry["claim"]
        pages.append(
            PageClaim(
                page_index=page_index,
                offset=entry["header_end"],
                header_offset=entry["header_pos"],
                num_values=entry["num_values"],
                claim=Claim(
                    has_min_max=claim.has_min_max,
                    null_count=claim.null_count,
                    has_null_count=claim.has_null_count,
                    min_claim=claim.min_claim,
                    max_claim=claim.max_claim,
                    min_truncated=claim.min_truncated,
                    max_truncated=claim.max_truncated,
                    num_values=entry["num_values"],
                ),
                raw_header=entry["nodes"],
                # 物理跨度（页头声明大小 + 可能存在的 CRC），重建时复制
                compressed_size=entry["csize"] + (4 if entry["has_crc"] else 0),
                # 页头声明的数据大小（不含 CRC）
                data_size=entry["csize"],
            )
        )
        page_index += 1
    return pages


# ---------------------------------------------------------------- 实际数据


def read_column_values(
    path: str | Path, row_group: int, column_path: str
) -> list[Any]:
    """读取一个列块的实际值列表（NULL 用 None 表示）。

    这是审计的"事实来源"，不读取文件自报统计。
    """
    table = pq.read_table(path, columns=[column_path])
    chunked = table.column(column_path)
    if row_group >= chunked.num_chunks:
        raise ParquetFormatError(
            f"{path}: 行组 {row_group} 超出列 {column_path} 的 chunk 数"
        )
    return chunked.chunk(row_group).to_pylist()


def page_value_slices(model: FileModel, chunk: ChunkClaim, values: list[Any]) -> list[list[Any]]:
    """按数据页 num_values 顺序切分实际值（仅适用于扁平列）。"""
    slices: list[list[Any]] = []
    pos = 0
    for page in chunk.pages:
        n = page.num_values
        slices.append(values[pos : pos + n])
        pos += n
    if pos != len(values):
        raise ParquetFormatError(
            f"{model.path}: 列 {chunk.path} 页 num_values 之和 "
            f"{pos} 与实际值数 {len(values)} 不一致"
        )
    return slices


# ---------------------------------------------------------------- 改写


@dataclass
class StatsPatch:
    """对单个位置统计的改写指令。"""

    row_group: int
    column_path: str
    page_index: int | None = None  # None 表示改列块统计
    min_value: Any = None
    max_value: Any = None
    null_count: int | None = None
    set_min_truncated: bool | None = None
    set_max_truncated: bool | None = None
    clear_stats: bool = False
    physical_type: str = ""


def _set_or_remove(nodes: list[tc.TNode], fid: int, value: object, ctype: int) -> None:
    for i, n in enumerate(nodes):
        if n.fid == fid:
            if value is None:
                del nodes[i]
            else:
                nodes[i] = tc.TNode(fid, value, ctype=ctype)
            return
    if value is not None:
        nodes.append(tc.TNode(fid, value, ctype=ctype))


def _apply_stats_patch(
    stats_nodes: list[tc.TNode] | None, patch: StatsPatch
) -> list[tc.TNode] | None:
    if patch.clear_stats:
        return None
    nodes = list(stats_nodes or [])
    if patch.min_value is not None:
        _set_or_remove(
            nodes,
            _ST_MIN_V2,
            encode_stat_bytes(patch.min_value, patch.physical_type),
            tc._CT_BINARY,
        )
    if patch.max_value is not None:
        _set_or_remove(
            nodes,
            _ST_MAX_V2,
            encode_stat_bytes(patch.max_value, patch.physical_type),
            tc._CT_BINARY,
        )
    if patch.null_count is not None:
        _set_or_remove(nodes, _ST_NULL_COUNT, patch.null_count, tc._CT_I64)

    def _bool(fid: int, flag: bool) -> None:
        _set_or_remove(
            nodes, fid, flag, tc._CT_TRUE if flag else tc._CT_FALSE
        )

    if patch.set_min_truncated is not None:
        # 写入的是 is_min_value_exact = 非截断
        _bool(_ST_MIN_TRUNC, not patch.set_min_truncated)
    if patch.set_max_truncated is not None:
        _bool(_ST_MAX_TRUNC, not patch.set_max_truncated)
    return nodes


def rewrite_file(
    model: FileModel,
    patches: list[StatsPatch],
    sorting_override: list[SortingClaim] | None = None,
    remove_chunk_stats_paths: set[str] | None = None,
) -> bytes:
    """按补丁重建 Parquet 文件字节。

    页头改写后页头长度可能变化，因此重新拼接所有列块并同步更新
    ColumnChunk 的 data_page_offset 与 total_compressed_size；最后
    重写页脚长度。若列块内出现字典页（offset_index 之外），一并保留。
    """
    raw = model.raw
    patches_by_chunk: dict[tuple[int, str], list[StatsPatch]] = {}
    for p in patches:
        patches_by_chunk.setdefault((p.row_group, p.column_path), []).append(p)

    # 对每个 row group 重建列块字节流，记录新的偏移/大小/页头尺寸增量
    new_chunks_data: dict[
        tuple[int, str], tuple[bytes, int, int, int]
    ] = {}
    for rg_model in model.row_groups:
        for chunk in rg_model.chunks:
            chunk_patches = patches_by_chunk.get(
                (rg_model.row_group_index, chunk.path), []
            )
            page_patches = {
                p.page_index: p for p in chunk_patches if p.page_index is not None
            }
            new_bytes = bytearray()
            pos = chunk.data_offset
            end = pos + chunk.total_compressed_size
            header_delta = 0
            for page in chunk.pages:
                # 保留页头之前可能存在的非数据页（字典页）字节
                new_bytes += raw[pos:page.header_offset]
                # 深拷贝页头节点，避免就地修改污染 model（同一 model 可能
                # 被多次 rewrite_file 复用来生成不同夹具）
                nodes = tc.clone_nodes(page.raw_header)
                ph = tc.TNode(None, nodes)
                page_will_change = page.page_index in page_patches
                if page_will_change:
                    patch = page_patches[page.page_index]
                    dp1 = ph.get(_PH_DATA_PAGE_V1)
                    dp2 = ph.get(_PH_DATA_PAGE_V2)
                    target = dp1 if dp1 is not None else dp2
                    stats_fid = _DP1_STATS if dp1 is not None else _DP2_STATS
                    dp_nodes = list(target.value)
                    current_stats = None
                    stats_node = tc.TNode(None, dp_nodes).get(stats_fid)
                    if stats_node is not None:
                        current_stats = stats_node.value
                    updated = _apply_stats_patch(current_stats, patch)
                    _set_or_remove(
                        dp_nodes, stats_fid, updated, tc._CT_STRUCT
                    )
                    # 把改后的 data_page_header 写回页头
                    new_dp = tc.TNode(
                        target.fid, dp_nodes, ctype=tc._CT_STRUCT
                    )
                    for i, n in enumerate(nodes):
                        if n.fid == target.fid:
                            nodes[i] = new_dp
                            break
                # 页头一旦变化，原 CRC（fid 4）立即失效：移除字段并丢弃
                # 数据尾部 4 字节 CRC，使 fid3 与实际数据长度一致。
                crc_idx = next(
                    (i for i, n in enumerate(nodes) if n.fid == _PH_CRC), None
                )
                strip_crc = page_will_change and crc_idx is not None
                if strip_crc:
                    del nodes[crc_idx]
                encoded_header = tc.encode_struct(nodes)
                old_header_len = page.offset - page.header_offset
                # fid7（total_uncompressed）只在页头被补丁实际修改时变化；
                # 未修改的页重编码为等价字节，不计入尺寸增量。
                if page_will_change:
                    header_delta += len(encoded_header) - old_header_len
                new_bytes += encoded_header
                # 页内容按物理跨度整段复制；仅当移除 CRC 字段时去掉尾部
                # 4 字节（该 CRC 对应已变化的页头，不再有效）。
                data_len = (
                    page.data_size if strip_crc else page.compressed_size
                )
                new_bytes += raw[page.offset : page.offset + data_len]
                pos = page.offset + page.compressed_size
            new_chunks_data[(rg_model.row_group_index, chunk.path)] = (
                bytes(new_bytes),
                len(new_bytes),
                0,  # 新偏移稍后分配
                header_delta,
            )

    # 线性重新布局列块（紧跟 4 字节魔数后，按原 row group/column 顺序）
    layout = bytearray(raw[:4])
    offsets: dict[tuple[int, str], int] = {}
    sizes: dict[tuple[int, str], int] = {}
    header_deltas: dict[tuple[int, str], int] = {}
    for rg_model in model.row_groups:
        for chunk in rg_model.chunks:
            key = (rg_model.row_group_index, chunk.path)
            data, size, _, hdelta = new_chunks_data[key]
            offsets[key] = len(layout)
            sizes[key] = size
            header_deltas[key] = hdelta
            layout += data

    # 改写页脚：列块偏移/大小、块级统计、排序声明
    # 深拷贝页脚节点树，所有修改只作用于副本
    footer_nodes = tc.clone_nodes(model.footer_nodes)
    for rg_idx, rg_nodes in enumerate(
        tc.TNode(None, footer_nodes).require(_FM_ROW_GROUPS).structs()
    ):
        if sorting_override is not None and rg_idx == 0:
            new_sc = [
                [
                    tc.TNode(_SC_COLUMN, s.column_idx, ctype=tc._CT_I32),
                    tc.TNode(
                        _SC_DESC,
                        bool(s.descending),
                        ctype=tc._CT_TRUE if s.descending else tc._CT_FALSE,
                    ),
                    tc.TNode(
                        _SC_NULLS_FIRST,
                        bool(s.nulls_first),
                        ctype=tc._CT_TRUE if s.nulls_first else tc._CT_FALSE,
                    ),
                ]
                for s in sorting_override
            ]
            _set_or_remove(rg_nodes, _RG_SORTING, tc.StructList(new_sc), tc._CT_LIST)

        rg_size_delta = 0
        for chunk_nodes in tc.TNode(None, rg_nodes).require(_RG_COLUMNS).structs():
            meta_index = next(i for i, n in enumerate(chunk_nodes) if n.fid == 3)
            meta_nodes = list(chunk_nodes[meta_index].value)
            meta = tc.TNode(None, meta_nodes)
            col_path = b".".join(
                meta.require(_CMD_PATH_IN_SCHEMA).scalars()
            ).decode("utf-8")
            key = (rg_idx, col_path)

            old_compressed = meta.require(_CMD_TOTAL_COMPRESSED).value
            rg_size_delta += sizes[key] - old_compressed

            _set_or_remove(meta_nodes, _CMD_DATA_OFFSET, offsets[key], tc._CT_I64)
            _set_or_remove(meta_nodes, _CMD_TOTAL_COMPRESSED, sizes[key], tc._CT_I64)
            # 未压缩大小：夹具不压缩，故与压缩大小同步变化
            old_uncompressed = meta.get(_CMD_TOTAL_UNCOMPRESSED)
            if old_uncompressed is not None:
                _set_or_remove(
                    meta_nodes,
                    _CMD_TOTAL_UNCOMPRESSED,
                    old_uncompressed.value + header_deltas[key],
                    tc._CT_I64,
                )

            block_patch = next(
                (
                    p
                    for p in patches_by_chunk.get(key, [])
                    if p.page_index is None
                ),
                None,
            )
            remove = (
                remove_chunk_stats_paths is not None
                and col_path in remove_chunk_stats_paths
            )
            if block_patch is not None or remove:
                cur_stats = None
                stats_node = meta.get(_CMD_STATS)
                if stats_node is not None:
                    cur_stats = stats_node.value
                updated = (
                    None if remove else _apply_stats_patch(cur_stats, block_patch)
                )
                _set_or_remove(meta_nodes, _CMD_STATS, updated, tc._CT_STRUCT)

            chunk_nodes[meta_index] = tc.TNode(
                3, meta_nodes, ctype=tc._CT_STRUCT
            )

        # RowGroup.total_byte_size（fid 2）同步更新
        rg_total = tc.TNode(None, rg_nodes).get(_RG_TOTAL_SIZE)
        if rg_total is not None:
            _set_or_remove(
                rg_nodes,
                _RG_TOTAL_SIZE,
                rg_total.value + rg_size_delta,
                tc._CT_I64,
            )

    new_footer = tc.encode_struct(footer_nodes)
    layout += new_footer
    layout += struct.pack("<I", len(new_footer))
    layout += PARQUET_MAGIC
    return bytes(layout)
