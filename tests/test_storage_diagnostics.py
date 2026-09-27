"""版本存储与诊断层测试：编码往返、摘要绑定、版本不可变、索引损坏检测。"""
from __future__ import annotations

import pytest

from app.diagnostics import describe_clusters, load_version
from app.errors import IndexCorruptionError, UnicodeVersionMismatchError
from app.indexing import build_index
from app.storage import decode_array, encode_array


@pytest.mark.parametrize(
    "values",
    [[], [0], [0, 1, 1, 3, 3, 7, 11], [0, 2_000_000, 2_000_001], [127, 128, 16383, 16384]],
)
def test_leb128_delta_roundtrip(values):
    enc = encode_array(values)
    assert isinstance(enc, bytes)
    assert decode_array(enc) == values


def test_encode_rejects_negative():
    with pytest.raises(ValueError):
        encode_array([1, 0])  # 非递减假设被破坏（delta 为负）


def test_version_persistence_and_immutability(service, store):
    raw = "a🇺🇳b".encode()
    idx = build_index(raw.decode())
    store.create_document("d1")
    store.save_version(
        doc_id="d1", version=0, index=idx,
        content_sha256="x" * 64,
        gcb_table_version="13.0.0", unidata_version="15.0.0",
        build_mode="full", parent_version=None, edit_info={"op": "create"},
    )
    store.save_content("d1", 0, raw)
    # 重复主键 → 状态冲突（sqlite IntegrityError）
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.save_version(
            doc_id="d1", version=0, index=idx,
            content_sha256="y" * 64,
            gcb_table_version="13.0.0", unidata_version="15.0.0",
            build_mode="full", parent_version=None, edit_info=None,
        )


def test_load_version_triple_check(service, store, settings):
    text = "x👨‍👩\r\ny"
    raw = text.encode()
    idx = build_index(text)
    store.create_document("d1")
    store.save_version(
        doc_id="d1", version=0, index=idx,
        content_sha256=__import__("hashlib").sha256(raw).hexdigest(),
        gcb_table_version=settings.gcb_table_version,
        unidata_version=settings.unidata_version,
        build_mode="full", parent_version=None, edit_info={"op": "create"},
    )
    store.save_content("d1", 0, raw)

    loaded = load_version(
        store, "d1", 0, raw_content=raw,
        expected_gcb=settings.gcb_table_version,
        expected_unidata=settings.unidata_version,
    )
    assert loaded.index.cluster_to_cp == idx.cluster_to_cp
    assert loaded.index.cp_to_byte == idx.cp_to_byte

    # 1) 原文摘要不一致
    with pytest.raises(IndexCorruptionError) as ei:
        load_version(
            store, "d1", 0, raw_content=b"different",
            expected_gcb=settings.gcb_table_version,
            expected_unidata=settings.unidata_version,
        )
    assert ei.value.code == "INDEX_CORRUPTION"
    assert "stored_sha256" in ei.value.details

    # 2) Unicode 版本不匹配
    with pytest.raises(UnicodeVersionMismatchError):
        load_version(
            store, "d1", 0, raw_content=raw,
            expected_gcb="99.0.0",
            expected_unidata=settings.unidata_version,
        )

    # 3) 存储索引数组被篡改（重算不符）
    conn = store._conn
    good = conn.execute("SELECT cp_to_byte FROM versions").fetchone()[0]
    tampered = encode_array([v + 1 for v in decode_array(good)])
    conn.execute("UPDATE versions SET cp_to_byte=?", (tampered,))
    conn.commit()
    with pytest.raises(IndexCorruptionError) as ei2:
        load_version(
            store, "d1", 0, raw_content=raw,
            expected_gcb=settings.gcb_table_version,
            expected_unidata=settings.unidata_version,
        )
    assert "cp_to_byte_mismatch" in ei2.value.details["problems"]


def test_describe_clusters_concrete(service, store):
    idx = build_index("a🇺🇳")
    rows = describe_clusters(idx)
    assert [r["cluster"] for r in rows] == [0, 1]
    assert rows[1]["text"] == "🇺🇳"
    assert rows[1]["codepoints"] == ["U+1F1FA", "U+1F1F3"]
    assert rows[1]["gcb_groups"] == ["Regional_Indicator", "Regional_Indicator"]
    assert rows[1]["byte_start"] == 1 and rows[1]["byte_end"] == 9
    assert rows[1]["byte_length"] == 8
