"""Index blob serialization + SQLite version storage tests."""

from __future__ import annotations

import array

import pytest

from textindex import index as index_mod
from textindex.errors import (
    CATEGORY_COMPUTATION_FAILURE,
    CATEGORY_STATE_CONFLICT,
    DigestMismatch,
    DocumentAlreadyExists,
    DocumentNotFound,
    IndexCorrupt,
    IndexVersionMismatch,
)
from textindex.storage import Storage

from . import fixtures


def test_pack_unpack_roundtrip_all_fixtures():
    for key, (text, expected) in fixtures.ALL_TEXT_FIXTURES.items():
        idx = index_mod.build_index(text)
        blob = index_mod.pack(idx)
        restored = index_mod.unpack(blob, text=text)
        assert list(restored.cp_start) == expected["cluster_cp_starts"]
        assert list(restored.byte_start) == expected["cluster_byte_starts"]
        assert restored.clusters() == expected["clusters"]


def test_blob_checksum_detects_flip(recorder):
    rec, counts = recorder
    idx = index_mod.build_index("abcdef")
    blob = bytearray(index_mod.pack(idx))
    # flip a payload byte (well after the 80+ byte header)
    flip_at = len(blob) - 3
    blob[flip_at] ^= 0xFF
    try:
        index_mod.unpack(bytes(blob), text="abcdef")
        passed = False
    except IndexCorrupt as exc:
        passed = exc.category == CATEGORY_COMPUTATION_FAILURE and \
            "checksum" in exc.details["reason"]
        counts["PASS" if passed else "FAIL"] += 1
        rec.judge(test="blob_checksum_flip", kind="negative", passed=passed,
                  expected="index_corrupt/checksum", actual=exc.code,
                  intermediate={"flipped_byte": flip_at},
                  reason="single-bit payload change must fail SHA-256",
                  error_category=exc.category, error_code=exc.code)
    assert passed


def test_blob_rejects_truncation():
    blob = index_mod.pack(index_mod.build_index("abc"))
    with pytest.raises(IndexCorrupt):
        index_mod.unpack(blob[:20], text="abc")
    with pytest.raises(IndexCorrupt):
        index_mod.unpack(blob[:-2], text="abc")


def test_blob_bad_magic():
    blob = bytearray(index_mod.pack(index_mod.build_index("abc")))
    blob[0] ^= 0x01
    with pytest.raises(IndexCorrupt) as ei:
        index_mod.unpack(bytes(blob), text="abc")
    assert ei.value.details["reason"] == "bad magic"


def test_blob_text_rebinding_validates_length(recorder):
    rec, counts = recorder
    idx = index_mod.build_index("abc")
    blob = index_mod.pack(idx)
    try:
        index_mod.unpack(blob, text="abcd")  # wrong text length
        passed = False
    except IndexCorrupt:
        passed = True
    counts["PASS" if passed else "FAIL"] += 1
    rec.judge(test="blob_wrong_text", kind="negative", passed=passed,
              expected="index_corrupt", actual="index_corrupt",
              reason="blob offsets must not validate against different text")


def test_blob_version_identity_is_explicit():
    blob = index_mod.pack(index_mod.build_index("a"))
    # identity string is embedded verbatim after the header
    assert b"blob1:grapheme-0.6.0/unicode-13.0.0" in blob


def test_storage_create_get_version_history(service, recorder):
    rec, counts = recorder
    doc = service.create_document("aé\U0001F600", doc_id="d1")
    assert doc["revision"] == 0
    assert doc["unicode_version"] == "13.0.0"
    fetched = service.get_document("d1")
    passed = fetched["digest"] == doc["digest"] and \
        fetched["cluster_count"] == 3
    counts["PASS" if passed else "FAIL"] += 1
    rec.judge(test="storage_roundtrip", kind="state", passed=passed,
              expected={"digest": doc["digest"], "clusters": 3},
              actual={"digest": fetched["digest"],
                      "clusters": fetched["cluster_count"]},
              reason="reloaded blob must decode against stored text")

    service.edit_document("d1", start=1, end=1, replacement="́",
                          unit="grapheme")
    versions = service.list_versions("d1")
    assert [v["revision"] for v in versions] == [0, 1]
    assert versions[0]["text_sha256"] != versions[1]["text_sha256"]
    assert all(v["index_version"].startswith("grapheme-") for v in versions)


def test_storage_duplicate_and_missing(service):
    service.create_document("hi", doc_id="dup")
    with pytest.raises(DocumentAlreadyExists) as ei:
        service.create_document("ho", doc_id="dup")
    assert ei.value.category == CATEGORY_STATE_CONFLICT
    with pytest.raises(DocumentNotFound):
        service.get_document("nope")


def test_digest_mismatch_blocks_stale_edit(service, recorder):
    rec, counts = recorder
    service.create_document("abc", doc_id="dm")
    try:
        service.edit_document("dm", start=0, end=1, replacement="z",
                              unit="grapheme",
                              base_digest="0" * 64)
        passed = False
    except DigestMismatch as exc:
        passed = exc.category == CATEGORY_STATE_CONFLICT and \
            exc.http_status == 409
        counts["PASS" if passed else "FAIL"] += 1
        rec.judge(test="digest_mismatch", kind="state_conflict",
                  passed=passed, expected="digest_mismatch/409",
                  actual=f"{exc.code}/{exc.http_status}",
                  intermediate={"provided": exc.details["provided"][:12],
                                "current": exc.details["current"][:12]},
                  reason="stale base digest must block the edit",
                  error_category=exc.category, error_code=exc.code)
    assert passed
    # document untouched after the refused edit
    assert service.get_document("dm")["text"] == "abc"


def test_version_mismatch_on_tampered_identity(settings):
    store = Storage(settings.db_path)
    try:
        idx = index_mod.build_index("abc")
        store.create("v1", "abc", "NFC", idx)
        # tamper the stored identity string directly
        store.conn.execute(
            "UPDATE documents SET index_version='grapheme-9.9.9/unicode-1.0' "
            "WHERE doc_id='v1'")
        store.conn.commit()
        with pytest.raises(IndexVersionMismatch) as ei:
            store.get("v1")
        assert ei.value.category == CATEGORY_STATE_CONFLICT
    finally:
        store.close()


def test_reopened_index_survives_new_connection(settings):
    s1 = Storage(settings.db_path)
    s1.create("persist", "héllo", "NFC", index_mod.build_index("héllo"))
    s1.close()
    s2 = Storage(settings.db_path)
    try:
        doc = s2.get("persist")
        assert doc.index.cluster_count == 5
        assert list(doc.index.byte_start) == [0, 1, 3, 4, 5, 6]
    finally:
        s2.close()
