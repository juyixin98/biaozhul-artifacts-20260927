"""Thrift compact 编解码器的字节级往返与字段保持测试。"""
from __future__ import annotations

import struct

import pyarrow as pa
import pyarrow.parquet as pq

from colstats import parquet_codec as tc


def _write_tmp(path, table, **kw):
    pq.write_table(
        table, path, version="2.6", use_dictionary=False, column_encoding="PLAIN",
        compression="NONE", write_page_index=False, write_page_checksum=False, **kw
    )


def test_footer_roundtrip_byte_identical(tmp_path):
    t = pa.table({
        "x": pa.array(list(range(20000)), pa.int32()),
        "s": pa.array([f"v{i % 500}" for i in range(20000)], pa.string()),
        "f": pa.array([1.0, float("nan"), -0.0, 0.0] * 5000, pa.float64()),
    })
    p = tmp_path / "f.parquet"
    _write_tmp(p, t, row_group_size=10000, data_page_size=512)
    raw = p.read_bytes()
    flen = struct.unpack("<I", raw[-8:-4])[0]
    footer = raw[-8 - flen:-8]
    nodes, consumed = tc.decode_struct(footer)
    assert consumed == flen
    assert tc.encode_struct(nodes) == footer  # 字节级无损


def test_scalar_and_struct_lists_preserved():
    # 手工构造含 scalar 列表（i32 列表）与 struct 列表的消息
    nodes = [
        tc.TNode(1, 7, ctype=tc._CT_I32),
        tc.TNode(2, tc.ScalarList([3, 0], tc._CT_I32)),
        tc.TNode(
            3,
            tc.StructList([
                [tc.TNode(1, 1, ctype=tc._CT_I32)],
                [tc.TNode(1, 2, ctype=tc._CT_I32)],
            ]),
        ),
        tc.TNode(4, tc.ScalarList([], tc._CT_I64)),  # 空列表也保留 etype
        tc.TNode(5, True, ctype=tc._CT_TRUE),
        tc.TNode(6, False, ctype=tc._CT_FALSE),
        tc.TNode(7, 1.5, ctype=tc._CT_DOUBLE),
    ]
    raw = tc.encode_struct(nodes)
    back, _ = tc.decode_struct(raw)
    assert tc.encode_struct(back) == raw
    root = tc.TNode(None, back)
    assert root.require(2).scalars() == [3, 0]
    assert len(root.require(3).structs()) == 2
    assert root.require(4).scalars() == []
    assert root.require(5).value is True
    assert root.require(6).value is False
    assert root.require(7).value == 1.5


def test_zigzag_mapping():
    assert tc._zz_dec(tc._zz_enc(0)) == 0
    assert tc._zz_dec(tc._zz_enc(-1)) == -1
    assert tc._zz_dec(tc._zz_enc(1)) == 1
    assert tc._zz_dec(tc._zz_enc(2**40)) == 2**40
    assert tc._zz_dec(tc._zz_enc(-(2**40))) == -(2**40)


def test_unknown_fields_kept(tmp_path):
    # 往返一个真实文件，未知字段（如未来版本扩展）不会丢失
    t = pa.table({"x": pa.array([1, None, 3], pa.int32())})
    p = tmp_path / "u.parquet"
    _write_tmp(p, t)
    raw = p.read_bytes()
    flen = struct.unpack("<I", raw[-8:-4])[0]
    footer = raw[-8 - flen:-8]
    nodes, _ = tc.decode_struct(footer)
    assert tc.encode_struct(nodes) == footer
