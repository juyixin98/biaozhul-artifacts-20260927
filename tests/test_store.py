"""Versioned store tests: hashes, rebuild-on-change, saved queries."""

from __future__ import annotations

import json

from searchdsl.store import Store, sha256_bytes


def _write(tmp, corpus, schema=None):
    schema = schema or {
        "fields": {"t": {"type": "text"}, "n": {"type": "int"}},
        "default_fields": ["t"],
    }
    sp = tmp / "schema.json"
    cp = tmp / "corpus.jsonl"
    sp.write_text(json.dumps(schema), encoding="utf-8")
    cp.write_text("\n".join(json.dumps(d) for d in corpus), encoding="utf-8")
    return sp, cp


def test_build_records_versions(tmp_path):
    sp, cp = _write(tmp_path, [{"doc_id": "x", "fields": {"t": "hello world"}}])
    store = Store(":memory:")
    v = store.build_from_files(sp, cp)
    assert v.dsl_version == "dsl-1.0"
    assert v.index_schema_version == "index-1.0"
    assert v.corpus_version == sha256_bytes(cp.read_bytes())
    assert v.doc_count == 1


def test_unchanged_corpus_does_not_rebuild(tmp_path):
    sp, cp = _write(tmp_path, [{"doc_id": "x", "fields": {"t": "a"}}])
    store = Store(":memory:")
    v1 = store.build_from_files(sp, cp)
    v2 = store.build_from_files(sp, cp)
    assert v1.built_at == v2.built_at


def test_changed_corpus_changes_version_and_answers(tmp_path):
    sp, cp = _write(tmp_path, [{"doc_id": "x", "fields": {"t": "alpha"}}])
    store = Store(":memory:")
    v1 = store.build_from_files(sp, cp)
    assert store.term_docs("t", "alpha") == {"x"}

    cp.write_text(json.dumps({"doc_id": "y", "fields": {"t": "beta"}}),
                  encoding="utf-8")
    v2 = store.build_from_files(sp, cp)
    assert v2.corpus_version != v1.corpus_version
    assert store.term_docs("t", "beta") == {"y"}
    assert store.term_docs("t", "alpha") == set()  # old index state gone


def test_changed_schema_changes_version(tmp_path):
    sp, cp = _write(tmp_path, [{"doc_id": "x", "fields": {"t": "a", "n": 1}}])
    store = Store(":memory:")
    v1 = store.build_from_files(sp, cp)
    _write(tmp_path,
           [{"doc_id": "x", "fields": {"t": "a", "n": 1}}],
           schema={"fields": {"t": {"type": "text"}, "n": {"type": "int"},
                              "tag": {"type": "keyword"}},
                   "default_fields": ["t"]})
    v2 = store.build_from_files(sp, cp)
    assert v2.schema_version != v1.schema_version


def test_saved_query_is_idempotent_by_hash(tmp_path):
    sp, cp = _write(tmp_path, [{"doc_id": "x", "fields": {"t": "fox dog"}}])
    store = Store(":memory:")
    v = store.build_from_files(sp, cp)
    from searchdsl.parser import parse
    from searchdsl.normalize import normalize
    from searchdsl.astnodes import canonical_hash, canonical_json

    c1 = normalize(parse("fox dog"))
    c2 = normalize(parse("dog AND fox"))
    h = canonical_hash(c1)
    assert h == canonical_hash(c2)
    assert store.save_query(h, canonical_json(c1), source="fox dog", version=v) is True
    # Same hash again is a no-op.
    assert store.save_query(h, canonical_json(c2), source="dog AND fox", version=v) is False
    row = store.get_saved_query(h)
    assert row["corpus_version"] == v.corpus_version
    assert store.get_saved_query("0" * 64) is None


def test_duplicate_doc_ids_rejected(tmp_path):
    sp, cp = _write(tmp_path, [
        {"doc_id": "x", "fields": {"t": "a"}},
        {"doc_id": "x", "fields": {"t": "b"}},
    ])
    store = Store(":memory:")
    try:
        store.build_from_files(sp, cp)
    except ValueError as exc:
        assert "duplicate doc_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected duplicate doc_id rejection")
