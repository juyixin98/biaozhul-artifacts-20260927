"""Hand-written Parquet v1 reader/writer for the restricted type surface.

The writer serialises the kernel's leaf events into standard Parquet data
pages (PLAIN values, RLE/bit-packed hybrid levels, uncompressed) with a fully
populated Thrift footer, so files are readable by PyArrow. The reader parses
such files back into kernel events, reconstructing the (DL, RL, value) stream
per leaf with page/position diagnostics on malformed input.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from ..kernel.levels import EncodedColumn, LeafEvent, encode_records, decode_records
from ..kernel.pages import plan_pages
from ..kernel.schema import (
    LeafColumn, ListNode, OPTIONAL, PhysicalType, PrimitiveNode, REQUIRED,
    REPEATED, RootNode, SchemaNode, StructNode,
)
from . import parquet_thrift as pt
from .encodings import bit_width_for, decode_levels, frame_levels
from .plain import decode_values, encode_values
from .thrift import decode_struct, encode_struct

PARQUET_MAGIC = b"PAR1"
CREATED_BY = "pnv-handwritten/1.0"


class ParquetWriteError(ValueError):
    pass


class ParquetReadError(ValueError):
    def __init__(self, message, page_index=None, position=None, column_path=""):
        super().__init__(message)
        self.page_index = page_index
        self.position = position
        self.column_path = column_path


# --------------------------------------------------------------------------- #
# Schema <-> Parquet thrift SchemaElement
# --------------------------------------------------------------------------- #

_PHYSICAL_TYPE_ID = {
    PhysicalType.BOOLEAN: pt.Type_BOOLEAN,
    PhysicalType.INT32: pt.Type_INT32,
    PhysicalType.INT64: pt.Type_INT64,
    PhysicalType.FLOAT: pt.Type_FLOAT,
    PhysicalType.DOUBLE: pt.Type_DOUBLE,
    PhysicalType.BYTE_ARRAY: pt.Type_BYTE_ARRAY,
}

_REPETITION_ID = {
    REQUIRED: pt.FieldRepetitionType_REQUIRED,
    OPTIONAL: pt.FieldRepetitionType_OPTIONAL,
    REPEATED: pt.FieldRepetitionType_REPEATED,
}


def schema_to_elements(root: RootNode) -> list[dict]:
    elements: list[dict] = [{"name": root.name, "num_children": _child_count(root)}]

    def emit(node: SchemaNode) -> None:
        if isinstance(node, PrimitiveNode):
            el = {
                "type": _PHYSICAL_TYPE_ID[node.physical],
                "repetition_type": _REPETITION_ID[node.repetition],
                "name": node.name,
            }
            if node.logical == "STRING":
                el["converted_type"] = pt.ConvertedType_UTF8
            elements.append(el)
        elif isinstance(node, StructNode):
            elements.append({
                "repetition_type": _REPETITION_ID[node.repetition],
                "name": node.name,
                "num_children": len(node.fields),
            })
            for child in node.fields:
                emit(child)
        elif isinstance(node, ListNode):
            # canonical: optional/required group <name> (LIST) {
            #              repeated group list { <element> } }
            elements.append({
                "repetition_type": _REPETITION_ID[node.repetition],
                "name": node.name,
                "num_children": 1,
                "converted_type": pt.ConvertedType_LIST,
            })
            elements.append({
                "repetition_type": pt.FieldRepetitionType_REPEATED,
                "name": "list",
                "num_children": 1,
            })
            emit(node.element)

    for field in root.fields:
        emit(field)
    return elements


def _child_count(node: RootNode | StructNode | ListNode) -> int:
    if isinstance(node, RootNode):
        return len(node.fields)
    if isinstance(node, StructNode):
        return len(node.fields)
    return 0


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

@dataclass
class ColumnChunkResult:
    leaf: LeafColumn
    data_page_offset: int
    total_compressed_size: int
    num_values: int
    path: list[str]


def write_file(path: str, root: RootNode, records: list[dict],
               page_size_bytes: int = 1024,
               force_page_after_records: int | None = None) -> int:
    encoded = encode_records(root, records)
    return write_encoded_file(
        path, root, encoded.columns, encoded.num_records,
        page_size_bytes=page_size_bytes,
        force_page_after_records=force_page_after_records,
        empty_struct_present=encoded.empty_struct_present)


def write_encoded_file(path: str, root: RootNode,
                       columns: list[EncodedColumn], num_records: int,
                       page_size_bytes: int = 1024,
                       force_page_after_records: int | None = None,
                       empty_struct_present: dict | None = None) -> int:
    buf = bytearray(PARQUET_MAGIC)
    chunk_results: list[ColumnChunkResult] = []
    total_byte_size = 0

    for enc_col in columns:
        leaf = enc_col.leaf
        plans = plan_pages(enc_col, target_size_bytes=page_size_bytes,
                           force_page_after_records=force_page_after_records)
        column_start_offset = len(buf)
        num_values = len(enc_col.events)
        column_bytes = 0

        for page_index, plan in enumerate(plans):
            events = enc_col.events[plan.event_start:plan.event_end]
            page_bytes = _build_data_page(leaf, events, plan.record_count)
            buf += page_bytes
            column_bytes += len(page_bytes)

        path_schema = [p for p in leaf.path if p != "list"]
        # path_in_schema uses the user-visible field names: ids.element...
        path_schema = _path_in_schema(leaf.path)
        chunk_results.append(ColumnChunkResult(
            leaf=leaf,
            data_page_offset=column_start_offset,
            total_compressed_size=column_bytes,
            num_values=num_values,
            path=path_schema,
        ))
        total_byte_size += column_bytes

    row_group = {
        "columns": [_column_chunk_dict(c) for c in chunk_results],
        "total_byte_size": total_byte_size,
        "num_rows": num_records,
        "file_offset": chunk_results[0].data_page_offset if chunk_results else 0,
        "total_compressed_size": total_byte_size,
    }
    metadata = {
        "version": 1,
        "schema": schema_to_elements(root),
        "num_rows": num_records,
        "row_groups": [row_group],
        "created_by": CREATED_BY,
    }
    footer = encode_struct(pt.FileMetaData, metadata)
    buf += footer
    buf += len(footer).to_bytes(4, "little")
    buf += PARQUET_MAGIC
    with open(path, "wb") as f:
        f.write(buf)
    return len(buf)


def _path_in_schema(path: tuple[str, ...]) -> list[str]:
    # Drop the structural "list" group names; keep user field + "element".
    return [p for p in path if p != "list"]


def _column_chunk_dict(c: ColumnChunkResult) -> dict:
    encodings = [pt.Encoding_RLE, pt.Encoding_PLAIN]
    meta = {
        "type": _PHYSICAL_TYPE_ID[c.leaf.node.physical],
        "encodings": encodings,
        "path_in_schema": [p.encode("utf-8") for p in c.path],
        "codec": pt.CompressionCodec_UNCOMPRESSED,
        "num_values": c.num_values,
        "total_uncompressed_size": c.total_compressed_size,
        "total_compressed_size": c.total_compressed_size,
        "data_page_offset": c.data_page_offset,
    }
    # file_offset points to the end of the column chunk (including page
    # headers), as Parquet implementations record it.
    return {"file_offset": c.data_page_offset + c.total_compressed_size,
            "meta_data": meta}


def _build_data_page(leaf: LeafColumn, events: list[LeafEvent],
                     record_count: int) -> bytes:
    rep_levels = [ev.repetition_level for ev in events]
    def_levels = [ev.definition_level for ev in events]
    values = [ev.value for ev in events if ev.value is not None]
    encoded_values = encode_values(values, leaf.node.physical)

    # Match the de-facto Parquet v1 layout: a level section is emitted only
    # when its bit width is non-zero. A non-repeated leaf therefore has no
    # repetition-level section at all.
    rep_blob = (frame_levels(rep_levels,
                             bit_width_for(leaf.max_repetition_level))
                if leaf.max_repetition_level > 0 else b"")
    def_blob = (frame_levels(def_levels,
                             bit_width_for(leaf.max_definition_level))
                if leaf.max_definition_level > 0 else b"")
    payload = rep_blob + def_blob + encoded_values

    data_page_header = {
        "num_values": len(events),
        "encoding": pt.Encoding_PLAIN,
        "definition_level_encoding": pt.Encoding_RLE,
        "repetition_level_encoding": pt.Encoding_RLE,
    }
    page_header = {
        "type": pt.PageType_DATA_PAGE,
        "uncompressed_page_size": len(payload),
        "compressed_page_size": len(payload),
        "data_page_header": data_page_header,
    }
    header_bytes = encode_struct(pt.PageHeader, page_header)
    return header_bytes + payload


# --------------------------------------------------------------------------- #
# Reader
# --------------------------------------------------------------------------- #

@dataclass
class ReadPageInfo:
    page_index: int
    num_values: int
    first_record_index: int
    record_count: int


@dataclass
class ReadColumn:
    leaf: LeafColumn
    events: list[LeafEvent]
    pages: list[ReadPageInfo]


def read_file_events(path: str) -> tuple[RootNode, list[ReadColumn], int]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != PARQUET_MAGIC or data[-4:] != PARQUET_MAGIC:
        raise ParquetReadError("not a Parquet file (bad magic)")
    footer_length = struct.unpack_from("<I", data, len(data) - 8)[0]
    footer_start = len(data) - 8 - footer_length
    metadata, _ = decode_struct(pt.FileMetaData, data, footer_start,
                                len(data) - 8)
    root = _elements_to_schema(metadata["schema"])
    num_rows = metadata["num_rows"]

    leaves = {tuple(c.path): c for c in root_schema_leaves_public(root)}
    columns: list[ReadColumn] = []
    for rg in metadata["row_groups"]:
        for chunk in rg["columns"]:
            columns.append(_read_column_chunk(data, chunk, leaves))
    return root, columns, num_rows


def root_schema_leaves_public(root: RootNode) -> list[LeafColumn]:
    from ..kernel.schema import leaf_columns
    return leaf_columns(root)


def _elements_to_schema(elements: list[dict]) -> RootNode:
    # Decode bytes names to str.
    els = [{k: (v.decode("utf-8") if isinstance(v, bytes) else v)
            for k, v in el.items()} for el in elements]
    root_name = els[0]["name"]
    pos = 1

    def parse_field() -> SchemaNode:
        nonlocal pos
        el = els[pos]
        pos += 1
        name = el["name"]
        repetition = {
            pt.FieldRepetitionType_REQUIRED: REQUIRED,
            pt.FieldRepetitionType_OPTIONAL: OPTIONAL,
            pt.FieldRepetitionType_REPEATED: REPEATED,
        }[el["repetition_type"]] if "repetition_type" in el else OPTIONAL

        if el.get("converted_type") == pt.ConvertedType_LIST:
            # expect one repeated child "list"
            list_el = els[pos]
            pos += 1
            if list_el.get("name") != "list" or list_el.get(
                    "repetition_type") != pt.FieldRepetitionType_REPEATED:
                raise ParquetReadError(
                    f"LIST group {name!r} must contain a single repeated "
                    f"'list' group, found {list_el.get('name')!r}")
            element = parse_field()
            return ListNode(name=name, repetition=repetition, element=element)

        if "type" in el:
            physical = _physical_from_id(el["type"])
            logical = "STRING" if el.get("converted_type") == pt.ConvertedType_UTF8 else None
            _assert_supported_primitive(physical, logical, name, el)
            return PrimitiveNode(name=name, repetition=repetition,
                                 physical=physical, logical=logical)

        # struct group
        num_children = el.get("num_children", 0)
        children = tuple(parse_field() for _ in range(num_children))
        return StructNode(name=name, repetition=repetition, fields=children)

    fields: list[SchemaNode] = []
    num_top = els[0].get("num_children", 0)
    for _ in range(num_top):
        fields.append(parse_field())
    return RootNode(name=root_name, fields=tuple(fields))


def _physical_from_id(type_id: int) -> PhysicalType:
    for phys, tid in _PHYSICAL_TYPE_ID.items():
        if tid == type_id:
            return phys
    raise ParquetReadError(f"unsupported physical type id {type_id}")


def _assert_supported_primitive(physical, logical, name, el) -> None:
    if physical == PhysicalType.BYTE_ARRAY and logical != "STRING":
        raise ParquetReadError(
            f"field {name!r}: BYTE_ARRAY without STRING logical type is not "
            "supported by the restricted validator")


def _read_column_chunk(data: bytes, chunk: dict,
                       leaves: dict[tuple[str, ...], LeafColumn]) -> ReadColumn:
    meta = chunk.get("meta_data")
    if meta is None:
        raise ParquetReadError("column chunk without meta_data is not supported")
    if meta.get("codec") != pt.CompressionCodec_UNCOMPRESSED:
        raise ParquetReadError(
            f"compression codec {meta.get('codec')} is not supported "
            "(only UNCOMPRESSED)")
    path_tuple = tuple(
        p.decode("utf-8") if isinstance(p, bytes) else p
        for p in meta["path_in_schema"])
    leaf = _find_leaf_by_path(leaves, path_tuple)

    offset = meta["data_page_offset"]
    events: list[LeafEvent] = []
    pages: list[ReadPageInfo] = []
    page_index = 0
    first_record_index = 0
    payload_bytes_seen = 0
    total_payload = meta["total_compressed_size"]
    while payload_bytes_seen < total_payload:
        page_header, header_end = decode_struct(pt.PageHeader, data, offset,
                                                len(data) - 8)
        ptype = page_header.get("type")
        if ptype == pt.PageType_DATA_PAGE_V2:
            raise ParquetReadError(
                "data page v2 is not supported; write with data_page_version=1.0",
                page_index=page_index)
        if ptype != pt.PageType_DATA_PAGE:
            raise ParquetReadError(
                f"unsupported page type {ptype} (only plain v1 data pages)",
                page_index=page_index)
        dph = page_header["data_page_header"]
        if dph.get("encoding") != pt.Encoding_PLAIN:
            raise ParquetReadError(
                f"unsupported value encoding {dph.get('encoding')} (only PLAIN)",
                page_index=page_index)
        page_size = page_header["compressed_page_size"]
        payload = data[header_end:header_end + page_size]
        page_events = _decode_data_page(leaf, payload, dph["num_values"],
                                        page_index)
        record_count = sum(1 for ev in page_events if ev.repetition_level == 0)
        pages.append(ReadPageInfo(page_index, len(page_events),
                                  first_record_index, record_count))
        events.extend(page_events)
        first_record_index += record_count
        # total_compressed_size covers page headers AND payloads.
        payload_bytes_seen += (header_end - offset) + page_size
        offset = header_end + page_size
        page_index += 1

    if len(events) != meta["num_values"]:
        raise ParquetReadError(
            f"column {'.'.join(path_tuple)}: decoded {len(events)} values, "
            f"footer declares {meta['num_values']}")
    return ReadColumn(leaf=leaf, events=events, pages=pages)


def _find_leaf_by_path(leaves: dict[tuple[str, ...], LeafColumn],
                       schema_path: tuple[str, ...]) -> LeafColumn:
    # Footer path_in_schema lists every SchemaElement name including the
    # structural "list" group and the "element" node, which matches our leaf
    # paths exactly. Fall back to a structural comparison ignoring those
    # markers for tolerance of variant encoders.
    if schema_path in leaves:
        return leaves[schema_path]
    for full_path, leaf in leaves.items():
        if full_path == schema_path:
            return leaf
    # Last resort: same leaf ordinal position by comparing element-name tail.
    candidates = [leaf for path, leaf in leaves.items()
                  if path[-1] == schema_path[-1] and path[0] == schema_path[0]]
    if len(candidates) == 1:
        return candidates[0]
    raise ParquetReadError(
        f"no leaf column matches path {'.'.join(schema_path)}")


def _decode_data_page(leaf: LeafColumn, payload: bytes, num_values: int,
                      page_index: int) -> list[LeafEvent]:
    try:
        pos = 0
        rep_w = bit_width_for(leaf.max_repetition_level)
        def_w = bit_width_for(leaf.max_definition_level)
        # A level section exists on the wire only when its width is non-zero.
        if rep_w > 0:
            rep_blob_len = struct.unpack_from("<I", payload, pos)[0]
            rep = decode_levels(payload, rep_w, num_values, pos + 4)
            pos += 4 + rep_blob_len
        else:
            rep = type("D", (), {"levels": [0] * num_values})()
        if def_w > 0:
            def_blob_len = struct.unpack_from("<I", payload, pos)[0]
            dfl = decode_levels(payload, def_w, num_values, pos + 4)
            pos += 4 + def_blob_len
        else:
            dfl = type("D", (), {"levels": [0] * num_values})()

        present_count = sum(1 for dl in dfl.levels if dl == leaf.max_definition_level)
        values, _ = decode_values(payload, leaf.node.physical, present_count, pos)
        events: list[LeafEvent] = []
        vi = 0
        for rl, dl in zip(rep.levels, dfl.levels):
            value = None
            if dl == leaf.max_definition_level:
                value = values[vi]
                vi += 1
            events.append(LeafEvent(dl, rl, value))
        if vi != present_count:
            raise ParquetReadError("value count mismatch on page")
        return events
    except ParquetReadError:
        raise
    except (IndexError, struct.error, ValueError) as exc:
        raise ParquetReadError(
            f"malformed data page {page_index}: {exc}",
            page_index=page_index) from exc


def read_file_records(path: str,
                      empty_struct_present: dict[str, list[int]] | None = None
                      ) -> tuple[RootNode, list[dict], dict]:
    root, columns, num_rows = read_file_events(path)
    enc_columns = [EncodedColumn(c.leaf, c.events) for c in columns]
    records = decode_records(root, enc_columns, num_records=num_rows,
                             empty_struct_present=empty_struct_present)
    return root, records, {"num_rows": num_rows, "pages": columns}
