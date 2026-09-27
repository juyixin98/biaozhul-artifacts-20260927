"""SQLite storage tests: provenance graph, conflicts, resolutions."""

import pytest

from merge3.storage import VersionStore, sha256_text


@pytest.fixture()
def store():
    s = VersionStore(":memory:")
    yield s
    s.close()


def test_version_is_content_addressed_and_deduped(store):
    store.ensure_document("doc1")
    v1 = store.add_version("doc1", "base", "hello\n")
    v2 = store.add_version("doc1", "base", "hello\n")
    assert v1 == v2  # same content -> same id, inserted once
    got = store.get_version(v1)
    assert got["content"] == "hello\n"
    assert got["sha256"] == sha256_text("hello\n")
    assert got["char_count"] == 6


def test_merge_lifecycle_auto(store):
    store.ensure_document("d")
    b = store.add_version("d", "base", "a\nb\n")
    l = store.add_version("d", "local", "A\nb\n")
    r = store.add_version("d", "remote", "a\nB\n")
    store.record_merge(merge_id="mg1", request_id="req1", document_id="d",
                       base_version_id=b, local_version_id=l,
                       remote_version_id=r, status="auto",
                       merged_version_id=None)
    rec = store.get_merge("mg1")
    assert rec["status"] == "auto"
    assert rec["request_id"] == "req1"
    assert store.get_conflicts("mg1") == []


def test_conflicts_persist_three_way_ranges_and_choices(store):
    store.ensure_document("d")
    b = store.add_version("d", "base", "a\nb\nc\n")
    l = store.add_version("d", "local", "a\nc\n")
    r = store.add_version("d", "remote", "a\nB\nc\n")
    block = {
        "conflict_id": "c1",
        "conflict_type": "delete_modify",
        "base_region": {"document": "base", "start": 2, "end": 4,
                        "line_start": 1, "line_end": 2, "text": None},
        "local_region": {"document": "local", "start": 2, "end": 2,
                         "line_start": 1, "line_end": 1, "text": None},
        "remote_region": {"document": "remote", "start": 2, "end": 4,
                          "line_start": 1, "line_end": 2, "text": None},
        "base_text": "b\n", "local_text": "", "remote_text": "B\n",
        "local_edit_ids": ["l1"], "remote_edit_ids": ["r1"],
        "allowed_resolutions": ["local", "remote", "base", "custom_text"],
    }
    store.record_merge(merge_id="mg2", request_id="req2", document_id="d",
                       base_version_id=b, local_version_id=l,
                       remote_version_id=r, status="conflict",
                       conflicts=[block])
    got = store.get_conflicts("mg2")
    assert len(got) == 1
    c = got[0]
    assert c["conflict_type"] == "delete_modify"
    assert c["base_region"]["line_start"] == 1
    assert c["remote_text"] == "B\n"
    assert c["allowed_resolutions"][-1] == "custom_text"

    # resolve and verify transition
    mv = store.add_version("d", "merged", "a\nB\nc\n")
    store.attach_merged_version("mg2", mv)
    store.record_resolution("mg2", "c1", "remote", None)
    rec = store.get_merge("mg2")
    assert rec["status"] == "rebuilt"
    assert store.get_resolutions("mg2")[0]["choice"] == "remote"


def test_listing_and_foreign_keys(store):
    store.ensure_document("d")
    with pytest.raises(Exception):
        # bad role rejected
        store.add_version("d", "sideways", "x")
    b = store.add_version("d", "base", "x")
    l = store.add_version("d", "local", "y")
    rr = store.add_version("d", "remote", "z")
    store.record_merge(merge_id="mg3", request_id="req3", document_id="d",
                       base_version_id=b, local_version_id=l,
                       remote_version_id=rr, status="auto")
    assert [m["merge_id"] for m in store.list_merges("d")] == ["mg3"]
    assert store.get_merge("nope") is None
