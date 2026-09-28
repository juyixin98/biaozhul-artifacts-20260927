"""持久化、快照、版本不匹配、错误语义。"""
from __future__ import annotations

import sqlite3

import pytest

from app.config import Settings
from app.engine import Engine
from app.errors import (
    EntryNotFound,
    InvalidLimit,
    InvalidScore,
    InvalidSurface,
    NormalizerVersionMismatch,
)


def _bulk(client, corpus):
    r = client.post("/entries/bulk", json={"items": corpus})
    assert r.status_code == 200, r.text
    return r.json()


def test_data_survives_reopen(tmp_path, raw_corpus, log):
    settings = Settings.from_env(
        {"TRIE_DATA_DIR": str(tmp_path / "data"), "TRIE_LOG_DIR": str(tmp_path / "logs")}
    )
    eng = Engine(settings.db_path, settings.snapshot_dir)
    for r in raw_corpus:
        eng.upsert(r["id"], r["surface"], r["score"])
    before = [e.id for e in eng.complete("multi", 3).entries]
    rev = eng.store.revision()
    eng.close()

    eng2 = Engine(settings.db_path, settings.snapshot_dir)
    after = [e.id for e in eng2.complete("multi", 3).entries]
    assert after == before, "重开后查询结果变化"
    assert eng2.stats()["entries"] == len(raw_corpus)
    assert eng2.store.revision() == rev
    violations = eng2.trie.verify_integrity()
    assert violations == []
    eng2.close()
    log("PASS", "PASS", before=before, revision=rev, entries=len(raw_corpus))


def test_snapshot_create_and_restore(client, raw_corpus, log):
    _bulk(client, raw_corpus)
    snap = client.post("/snapshots", json={"name": "baseline", "note": "初始夹具"})
    assert snap.status_code == 200
    assert snap.json()["snapshot"]["entry_count"] == len(raw_corpus)

    # 写入新热词并删除旧词，使状态偏离快照。
    client.put("/entries/temp-1", json={"id": "temp-1", "surface": "multivariable", "score": 999})
    client.delete("/entries/mp-01")
    mid = client.get("/complete", params={"prefix": "", "k": 1}).json()["entries"][0]
    assert mid["id"] == "temp-1"

    r = client.post("/snapshots/baseline/restore")
    assert r.status_code == 200, r.text
    assert r.json()["rebuilt_entries"] == len(raw_corpus)
    top = client.get("/complete", params={"prefix": "", "k": 1}).json()["entries"][0]
    assert top["id"] == "ot-3", "恢复后内容与快照不一致"
    assert client.get("/health").json()["entries"] == len(raw_corpus)
    log("PASS", "PASS", snapshot="baseline", restored_top=top["id"])


def test_duplicate_snapshot_name_conflict(client, raw_corpus, log):
    _bulk(client, raw_corpus)
    assert client.post("/snapshots", json={"name": "s"}).status_code == 200
    r = client.post("/snapshots", json={"name": "s"})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "E_SNAPSHOT_CONFLICT"
    log("PASS", "PASS", code=body["error"]["code"])


def test_restore_missing_snapshot_is_404(client, raw_corpus, log):
    _bulk(client, raw_corpus)
    r = client.post("/snapshots/nope/restore")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_SNAPSHOT_NOT_FOUND"
    log("PASS", "PASS", code="E_SNAPSHOT_NOT_FOUND")


def test_normalizer_version_mismatch_rejects_stale_db(tmp_path, raw_corpus, log):
    settings = Settings.from_env(
        {"TRIE_DATA_DIR": str(tmp_path / "data"), "TRIE_LOG_DIR": str(tmp_path / "logs")}
    )
    eng = Engine(settings.db_path, settings.snapshot_dir)
    eng.upsert(raw_corpus[0]["id"], raw_corpus[0]["surface"], raw_corpus[0]["score"])
    eng.close()

    # 外部把 meta 中的规范化版本改旧，模拟二进制升级后打开旧索引。
    conn = sqlite3.connect(str(settings.db_path))
    conn.execute("UPDATE meta SET value='norm-0.9.0-old' WHERE key='normalizer_version'")
    conn.commit()
    conn.close()

    with pytest.raises(NormalizerVersionMismatch) as exc:
        Engine(settings.db_path, settings.snapshot_dir)
    assert exc.value.details["stored"] == "norm-0.9.0-old"
    log("PASS", "PASS", code="E_NORMALIZER_VERSION_MISMATCH",
        stored=exc.value.details["stored"], current=exc.value.details["current"])


def test_engine_validation_error_categories(tmp_path, log):
    settings = Settings.from_env(
        {"TRIE_DATA_DIR": str(tmp_path / "data"), "TRIE_LOG_DIR": str(tmp_path / "logs")}
    )
    eng = Engine(settings.db_path, settings.snapshot_dir)
    with pytest.raises(InvalidSurface):
        eng.upsert("x", "   ", 1)
    with pytest.raises(InvalidSurface):
        eng.upsert("x", "abc\u0000def", 1)
    with pytest.raises(InvalidScore):
        eng.upsert("x", "abc", -1)
    with pytest.raises(InvalidScore):
        eng.upsert("x", "abc", True)  # type: ignore[arg-type]
    with pytest.raises(InvalidLimit):
        eng.complete("a", 0)
    with pytest.raises(InvalidLimit):
        eng.complete("a", 10000)
    with pytest.raises(EntryNotFound):
        eng.delete("ghost")
    with pytest.raises(EntryNotFound):
        eng.set_score("ghost", 5)
    eng.upsert("x", "abc", 5)
    with pytest.raises(InvalidScore):
        eng.adjust_score("x", -9)
    log("PASS", "PASS", categories=["InvalidSurface", "InvalidScore", "InvalidLimit", "EntryNotFound"])
    eng.close()


def test_write_persistence_ordering_and_revision(client, raw_corpus, log):
    r1 = _bulk(client, raw_corpus)
    assert r1["revision"] >= 1
    r2 = client.post("/entries/mp-01/adjust", json={"delta": -5})
    assert r2.status_code == 200 and r2.json()["entry"]["score"] == 90
    assert r2.json()["entry"]["surface"] == "multiprocessing"
    r3 = client.post("/entries/mp-01/adjust", json={"delta": -1000})
    assert r3.status_code == 400 and r3.json()["error"]["code"] == "E_INVALID_SCORE"
    log("PASS", "PASS", revision_after_bulk=r1["revision"], adjust_result=90)
