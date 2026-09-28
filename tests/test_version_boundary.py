"""显式版本边界：切换只重置节点、保留字节偏移，且不产生跨边界命中。"""

from __future__ import annotations

import pytest

from acstream.diagnostics import Diagnostics
from acstream.errors import ApiError, ErrorCode
from acstream.services.matcher import MatcherService
from acstream.services.version_registry import VersionRegistry
from acstream.storage.db import DiagnosticStore, connect, init_db


@pytest.fixture
def service(tmp_path):
    conn = connect(tmp_path / "v.db")
    init_db(conn)
    registry = VersionRegistry(conn)
    diag = Diagnostics(DiagnosticStore(conn))
    svc = MatcherService(
        conn, registry, diag,
        cursor_secret=b"unit-test-secret-key-0123456789ab",
    )
    return svc, registry


def test_switch_resets_node_but_keeps_offset(service) -> None:
    svc, registry = service
    v1, _ = registry.create([("ab", b"ab")], "utf-8")
    v2, auto2 = registry.create([("bc", b"bc")], "utf-8")
    sid = svc.open_session(v1)["sid"]

    # 喂入 "a"：在 v1 中停在 trie 内部节点（部分匹配）。
    r = svc.feed(sid, b"a", expected_offset=None,
                 expected_fingerprint=None, finish=False)
    assert r["state"]["byte_offset"] == 1
    assert r["state"]["node_state"] != 0  # 确实离开了根
    assert r["new_hits"] == 0

    # 显式边界切到 v2：节点必须归零，偏移保留为 1。
    switched = svc.switch_version(sid, v2)
    assert switched["state"]["node_state"] == 0
    assert switched["state"]["byte_offset"] == 1
    assert switched["version_id"] == v2

    # 继续喂 "bc"：若错误沿用 v1 的内部节点，"bc" 不会命中；
    # 正确重置后 bc 在整流绝对字节偏移 [1,3) 处命中（b 位于偏移 1）。
    r2 = svc.feed(sid, b"bc", expected_offset=1,
                  expected_fingerprint=None, finish=False)
    page = svc.list_hits_page(sid, cursor=None, limit=100)
    assert [(i["pattern_id"], i["start"], i["end"]) for i in page["items"]] == [
        ("bc", 1, 3)
    ]
    assert r2["state"]["byte_offset"] == 3


def test_switch_to_missing_version_is_reject(service) -> None:
    svc, registry = service
    v1, _ = registry.create([("a", b"a")], "utf-8")
    sid = svc.open_session(v1)["sid"]
    with pytest.raises(ApiError) as exc:
        svc.switch_version(sid, "ver_does_not_exist")
    assert exc.value.code == ErrorCode.VERSION_NOT_FOUND
    # 状态未被破坏。
    assert svc.get_state(sid)["version_id"] == v1


def test_feed_after_finish_rejected(service) -> None:
    svc, registry = service
    v1, _ = registry.create([("a", b"a")], "utf-8")
    sid = svc.open_session(v1)["sid"]
    svc.feed(sid, b"a", expected_offset=None,
             expected_fingerprint=None, finish=True)
    with pytest.raises(ApiError) as exc:
        svc.feed(sid, b"a", expected_offset=None,
                 expected_fingerprint=None, finish=False)
    assert exc.value.code == ErrorCode.SESSION_FINISHED
    # 历史命中仍可读。
    assert svc.list_hits_page(sid, cursor=None, limit=10)["items"]


def test_offset_mismatch_leaves_state_untouched(service) -> None:
    svc, registry = service
    v1, _ = registry.create([("a", b"a")], "utf-8")
    sid = svc.open_session(v1)["sid"]
    svc.feed(sid, b"aaa", expected_offset=None,
             expected_fingerprint=None, finish=False)
    before = svc.get_state(sid)
    with pytest.raises(ApiError) as exc:
        svc.feed(sid, b"a", expected_offset=99,
                 expected_fingerprint=None, finish=False)
    assert exc.value.code == ErrorCode.OFFSET_MISMATCH
    assert exc.value.outcome == "undetermined"
    after = svc.get_state(sid)
    assert after["state"] == before["state"]  # 块未被消费


def test_fingerprint_guard_rejects_stale_client(service) -> None:
    svc, registry = service
    v1, a1 = registry.create([("a", b"a")], "utf-8")
    v2, _ = registry.create([("b", b"b")], "utf-8")
    sid = svc.open_session(v1)["sid"]
    svc.switch_version(sid, v2)
    # 客户端仍以为在 v1，用旧指纹喂数据：必须被拒绝。
    with pytest.raises(ApiError) as exc:
        svc.feed(sid, b"b", expected_offset=0,
                 expected_fingerprint=a1.fingerprint, finish=False)
    assert exc.value.code == ErrorCode.FINGERPRINT_MISMATCH
    assert exc.value.outcome == "undetermined"


def test_identical_pattern_multiset_reuses_version(service) -> None:
    svc, registry = service
    id1, a1 = registry.create([("x", b"abc"), ("y", b"abd")], "utf-8")
    id2, a2 = registry.create([("y", b"abd"), ("x", b"abc")], "utf-8")
    assert id1 == id2 and a1.fingerprint == a2.fingerprint
