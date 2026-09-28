"""Resource exhaustion + computation failure category tests."""

from __future__ import annotations

import pytest

from textindex import index as index_mod
from textindex import segmenter
from textindex.config import Settings
from textindex.errors import (
    CATEGORY_COMPUTATION_FAILURE,
    CATEGORY_RESOURCE_EXHAUSTED,
    DocumentTooLarge,
    SegmenterError,
    TooManyClusters,
)
from textindex.service import TextIndexService
from textindex.storage import Storage
from textindex.diagnostics import RunLogger


def test_too_many_clusters_is_resource_exhausted(settings, recorder):
    rec, counts = recorder
    tight = Settings(db_path=settings.db_path,
                     log_path=settings.log_path,
                     max_document_bytes=1 << 20, max_clusters=3)
    store = Storage(tight.db_path)
    logger = RunLogger(tight.log_path)
    svc = TextIndexService(tight, storage=store, logger=logger)
    try:
        with pytest.raises(TooManyClusters) as ei:
            # 5 ascii codepoints = 5 clusters > 3
            svc.create_document("abcde", doc_id="big",
                                normalization="NONE")
        passed = ei.value.category == CATEGORY_RESOURCE_EXHAUSTED and \
            ei.value.http_status == 413 and \
            ei.value.details["clusters"] == 5 and \
            ei.value.details["limit"] == 3
        counts["PASS" if passed else "FAIL"] += 1
        rec.judge(test="too_many_clusters", kind="negative", passed=passed,
                  expected="resource_exhausted/too_many_clusters",
                  actual=f"{ei.value.category}/{ei.value.code}",
                  intermediate=ei.value.details,
                  reason="cluster cap must be a distinct 413 category",
                  error_category=ei.value.category, error_code=ei.value.code)
        assert passed
    finally:
        svc.close()


def test_too_many_clusters_on_edit(settings):
    store = Storage(settings.db_path)
    svc = TextIndexService(
        Settings(db_path=settings.db_path, log_path=settings.log_path,
                 max_document_bytes=1 << 20, max_clusters=3),
        storage=store, logger=RunLogger(settings.log_path))
    try:
        svc.create_document("ab", doc_id="t", normalization="NONE")
        with pytest.raises(TooManyClusters):
            svc.edit_document("t", start=2, end=2, replacement="cde",
                              unit="grapheme")
    finally:
        svc.close()


def test_document_too_large_on_edit(settings):
    # generous cluster cap, tight byte cap: size must be the binding limit.
    tight = Settings(db_path=settings.db_path,
                     log_path=settings.log_path,
                     max_document_bytes=100, max_clusters=1_000_000)
    store = Storage(tight.db_path)
    svc = TextIndexService(tight, storage=store,
                           logger=RunLogger(tight.log_path))
    try:
        svc.create_document("a", doc_id="g", normalization="NONE")
        with pytest.raises(DocumentTooLarge) as ei:
            svc.edit_document("g", start=1, end=1, replacement="x" * 500,
                              unit="grapheme")
        assert ei.value.category == CATEGORY_RESOURCE_EXHAUSTED
    finally:
        svc.close()


def test_segmenter_error_is_computation_failure(monkeypatch, recorder):
    rec, counts = recorder

    def boom(text):
        raise KeyError("synthesized property table failure")

    monkeypatch.setattr(segmenter.grapheme, "graphemes", boom)
    try:
        segmenter.cluster_spans("abc")
        passed = False
    except SegmenterError as exc:
        passed = exc.category == CATEGORY_COMPUTATION_FAILURE and \
            exc.http_status == 500
        counts["PASS" if passed else "FAIL"] += 1
        rec.judge(test="segmenter_error", kind="negative", passed=passed,
                  expected="computation_failure/segmenter_error",
                  actual=f"{exc.category}/{exc.code}",
                  reason="library exception must be wrapped, never raw",
                  error_category=exc.category, error_code=exc.code)
    assert passed


def test_invalid_index_blob_is_computation_failure():
    from textindex.errors import IndexCorrupt
    # Valid header/identity but tampered payload -> checksum failure,
    # distinct from the version-mismatch path.
    blob = bytearray(index_mod.pack(index_mod.build_index("abc")))
    blob[-1] ^= 0xFF
    with pytest.raises(IndexCorrupt) as ei:
        index_mod.unpack(bytes(blob), text="abc")
    assert ei.value.category == CATEGORY_COMPUTATION_FAILURE
    assert "checksum" in ei.value.details["reason"]


def test_storage_full_is_resource_exhausted(settings, monkeypatch, recorder):
    rec, counts = recorder
    import sqlite3
    from textindex.errors import StorageFull
    from textindex.storage import Storage as Store

    store = Store(settings.db_path)
    real_conn = store.conn

    class FullConn:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def execute(self_inner, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT"):
                err = sqlite3.OperationalError("disk I/O error / database full")
                err.sqlite_errorcode = 13
                raise err
            return real_conn.execute(sql, params)

        def executescript(self_inner, s):
            return real_conn.executescript(s)

    monkeypatch.setattr(store, "conn", FullConn())
    # `exists()` does a SELECT (allowed); the INSERT then blows up.
    try:
        store.create("full", "abc", "NONE", index_mod.build_index("abc"))
        passed = False
    except StorageFull as exc:
        passed = exc.category == CATEGORY_RESOURCE_EXHAUSTED and \
            exc.http_status == 507 and exc.code == "storage_full"
        counts["PASS" if passed else "FAIL"] += 1
        rec.judge(test="storage_full", kind="negative", passed=passed,
                  expected="resource_exhausted/storage_full",
                  actual=f"{exc.category}/{exc.code}",
                  reason="sqlite disk-full must map to 507, not 500",
                  error_category=exc.category, error_code=exc.code)
    finally:
        # restore real connection before close()
        monkeypatch.undo()
        store.close()
    assert passed
