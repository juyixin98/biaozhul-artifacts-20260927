"""编码与固定摘要测试：字节级黄金向量 + 与独立 oracle 的跨实现一致性。

黄金向量（哈希/长度/往返）由手写常量断言，不调用被测实现"自证"。
"""
from __future__ import annotations

import hashlib
import struct

import pytest

from utxo_ledger import encoding, fab
from utxo_ledger.errors import MalformedEncodingError


def test_varint_boundaries_known_vectors():
    # 手写边界向量
    assert encoding.varint(0) == b"\x00"
    assert encoding.varint(0xFC) == b"\xfc"
    assert encoding.varint(0xFD) == b"\xfd\x00\xfd"
    assert encoding.varint(0xFFFF) == b"\xfd\xff\xff"
    assert encoding.varint(0x10000) == b"\xfe\x00\x01\x00\x00"
    assert encoding.varint(0xFFFFFFFF) == b"\xfe\xff\xff\xff\xff"
    assert encoding.varint(0x100000000) == b"\xff" + struct.pack(">Q", 0x100000000)


def test_tx_body_byte_layout_golden(ring):
    tx = fab.issue_tx([(1000, ring.pub(0))])
    body = encoding.encode_tx_body(tx)
    # 布局偏移：
    # 0..3  魔数 UTXT
    # 4..7  u32 version
    # 8     varint nin
    # 9     varint nout
    # 10..17 u64 amount
    # 18    varint pubkey 长度 (33 -> 0x21)
    # 19..51 33B 压缩公钥
    # 52..59 u64 fee
    assert body[0:4] == b"UTXT"
    assert body[4:8] == struct.pack(">I", 1)
    assert body[8] == 0  # 0 inputs
    assert body[9] == 1  # 1 output
    assert body[10:18] == struct.pack(">Q", 1000)
    assert body[18] == 0x21
    assert body[19 : 19 + 33] == ring.pub(0)
    assert body[52:60] == b"\x00" * 8
    assert len(body) == 60


def test_txid_and_sighash_are_distinct_known_hashes(ring):
    tx = fab.issue_tx([(7, ring.pub(2))])
    body = encoding.encode_tx_body(tx)
    assert encoding.txid_of(tx) == hashlib.sha256(body).digest()
    assert encoding.sighash_of(tx) == hashlib.sha256(
        b"utxo-ledger/sighash/v1" + body
    ).digest()
    assert encoding.txid_of(tx) != encoding.sighash_of(tx)


def test_witness_not_part_of_txid(ring):
    base = fab.issue_tx([(5, ring.pub(0))])
    # issue 无输入；构造一笔带输入的 tx 验证改见证不改 txid
    tx_a = fab.unsigned_tx(
        [encoding.Outpoint(b"\x01" * 32, 0)],
        [fab.make_output(1, ring.pub(0))],
        fee=0,
    )
    tx_b = fab.with_witnesses(tx_a, [b"\x30\x06\x02\x01\x01\x02\x01\x01"])
    assert encoding.txid_of(tx_a) == encoding.txid_of(tx_b)
    assert encoding.encode_tx_full(tx_a) != encoding.encode_tx_full(tx_b)


def test_block_round_trip_binary_and_json(ring):
    g = fab.genesis_block([fab.issue_tx([(100, ring.pub(0))])])
    raw = encoding.encode_block(g)
    decoded = encoding.decode_block(raw)
    assert encoding.block_id_of(decoded) == encoding.block_id_of(g)
    assert decoded.transactions[0].outputs[0].amount == 100

    # JSON 往返
    js = encoding.block_to_json(g)
    again = encoding.block_from_json(js)
    assert encoding.encode_block(again) == raw


def test_decode_rejects_truncation_and_trailing_bytes(ring):
    g = fab.genesis_block([fab.issue_tx([(100, ring.pub(0))])])
    raw = encoding.encode_block(g)
    with pytest.raises(MalformedEncodingError) as ei:
        encoding.decode_block(raw[:-3])
    assert ei.value.code == "MALFORMED_ENCODING"
    with pytest.raises(MalformedEncodingError):
        encoding.decode_block(raw + b"\x00")


def test_json_strict_unknown_key_and_bool_int(ring):
    g = fab.genesis_block([fab.issue_tx([(100, ring.pub(0))])])
    js = encoding.block_to_json(g)
    js["header"]["extra"] = 1
    with pytest.raises(MalformedEncodingError) as ei:
        encoding.block_from_json(js)
    assert "extra" in ei.value.details["unknown"]

    js2 = encoding.block_to_json(g)
    js2["transactions"][0]["fee"] = True  # bool 不得作整数
    with pytest.raises(MalformedEncodingError):
        encoding.block_from_json(js2)


def test_encoding_matches_independent_oracle(ring, oracle):
    """同一笔交易：被测编码与 oracle 手写编码必须字节级相同。"""
    tx = fab.issue_tx([(123456, ring.pub(3)), (1, ring.pub(4))])
    js = encoding.tx_to_json(tx)
    assert oracle.tx_body(js) == encoding.encode_tx_body(tx)
    assert oracle.tx_full(js) == encoding.encode_tx_full(tx)
    assert oracle.txid(js) == encoding.txid_of(tx)

    g = fab.genesis_block([tx])
    bjs = encoding.block_to_json(g)
    assert oracle.header_bytes(bjs["header"]) == encoding.encode_header(g.header)
    assert oracle.block_id(bjs) == encoding.block_id_of(g)
    txr, wr = oracle.roots(bjs["transactions"])
    assert txr == g.header.tx_root
    assert wr == g.header.witness_root
