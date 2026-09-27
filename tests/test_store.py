"""版本存储测试:SQLite 持久化的读写一致性。"""

from app.store import Store


def test_version_roundtrip(tmp_path):
    store = Store(str(tmp_path / "s.sqlite3"))
    saved = store.save_version("doc1", "base", "a\r\nb\r\n")
    assert saved["line_ending"] == "crlf"
    assert saved["ends_with_newline"] is True
    row = store.get_version(saved["version_id"])
    assert row["content"] == "a\r\nb\r\n"
    assert row["role"] == "base"


def test_version_rejects_invalid_role(tmp_path):
    store = Store(str(tmp_path / "s.sqlite3"))
    try:
        store.save_version("doc1", "upstream", "x")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for invalid role")


def test_merge_record_roundtrip(tmp_path):
    store = Store(str(tmp_path / "s.sqlite3"))
    merge_id = store.save_merge(
        request_id="req-1",
        document_id="doc1",
        base="a\n",
        local="a\n",
        remote="a\nb\n",
        status="clean",
        result_text="a\nb\n",
        conflicts=[],
        notes=["some note"],
    )
    record = store.get_merge(merge_id)
    assert record["request_id"] == "req-1"
    assert record["status"] == "clean"
    assert record["result_text"] == "a\nb\n"
    assert record["resolved_text"] is None

    store.save_resolution(merge_id, "a\nb\n", {0: "remote"})
    record = store.get_merge(merge_id)
    assert record["resolved_text"] == "a\nb\n"


def test_get_missing_returns_none(tmp_path):
    store = Store(str(tmp_path / "s.sqlite3"))
    assert store.get_merge("nope") is None
    assert store.get_version("nope") is None
