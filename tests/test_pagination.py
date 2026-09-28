"""分页：大量单点命中的可恢复 keyset 分页、游标篡改与跨会话拒绝。"""

from __future__ import annotations

import pytest

from acstream.diagnostics import Diagnostics
from acstream.services.matcher import MatcherService
from acstream.services.version_registry import VersionRegistry
from acstream.storage.db import DiagnosticStore, connect, init_db


TEST_SECRET = b"unit-test-secret-key-0123456789ab"


def build_service(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    registry = VersionRegistry(conn)
    diag = Diagnostics(DiagnosticStore(conn), redact_payloads=True)
    service = MatcherService(
        conn,
        registry,
        diag,
        cursor_secret=TEST_SECRET,
        default_page_size=50,
        max_page_size=500,
    )
    return service, conn, registry


def test_dense_hits_pagination_recovers_every_hit(tmp_path) -> None:
    service, conn, registry = build_service(tmp_path)
    # 文本 a*1000，模式 a 与 aa：单点大量命中（1000 + 999 = 1999）。
    vid, _ = registry.create([("a", b"a"), ("aa", b"aa")], "utf-8")
    sess = service.open_session(vid)
    sid = sess["sid"]

    # 分多次喂入，确保分页跨 feed。
    service.feed(sid, b"a" * 400, expected_offset=None,
                 expected_fingerprint=None, finish=False)
    service.feed(sid, b"a" * 600, expected_offset=400,
                 expected_fingerprint=None, finish=True)

    seen: list[tuple] = []
    cursor = None
    pages = 0
    while True:
        page = service.list_hits_page(sid, cursor=cursor, limit=200)
        pages += 1
        seen.extend((it["pattern_id"], it["start"], it["end"]) for it in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"]
        cursor = page["next_cursor"]
        if pages > 20:
            pytest.fail("分页未终止")

    assert len(seen) == 1999
    # 有序且无重复。
    assert len(set(seen)) == len(seen)
    # 与朴素计数对照。
    assert sum(1 for t in seen if t[0] == "a") == 1000
    assert sum(1 for t in seen if t[0] == "aa") == 999


def test_cursor_tamper_rejected(tmp_path) -> None:
    from acstream.cursor import encode_cursor
    from acstream.errors import ApiError, ErrorCode

    service, conn, registry = build_service(tmp_path)
    vid, _ = registry.create([("a", b"a")], "utf-8")
    sid = service.open_session(vid)["sid"]
    service.feed(sid, b"aaa", expected_offset=None,
                 expected_fingerprint=None, finish=False)

    token = encode_cursor(
        b"unit-test-secret-key-0123456789ab",
        session_id=sid, end=1, start=0, pattern_id="a", seq=1,
    )
    # 用不同密钥构造的游标必须验签失败。
    forged = encode_cursor(
        b"another-secret-key-xxxxxxxxxxxxxx",
        session_id=sid, end=1, start=0, pattern_id="a", seq=1,
    )
    with pytest.raises(ApiError) as exc:
        service.list_hits_page(sid, cursor=forged, limit=10)
    assert exc.value.code == ErrorCode.CURSOR_INVALID

    # 体被改动（直接截断）也应失败。
    with pytest.raises(ApiError):
        service.list_hits_page(sid, cursor=token[:-2] + "AA", limit=10)


def test_cursor_cannot_cross_sessions(tmp_path) -> None:
    from acstream.cursor import encode_cursor
    from acstream.errors import ApiError, ErrorCode

    service, conn, registry = build_service(tmp_path)
    vid, _ = registry.create([("a", b"a")], "utf-8")
    s1 = service.open_session(vid)["sid"]
    s2 = service.open_session(vid)["sid"]
    token = encode_cursor(
        b"unit-test-secret-key-0123456789ab",
        session_id=s1, end=1, start=0, pattern_id="a", seq=1,
    )
    with pytest.raises(ApiError) as exc:
        service.list_hits_page(s2, cursor=token, limit=10)
    assert exc.value.code == ErrorCode.CURSOR_INVALID


def test_limit_validation(tmp_path) -> None:
    from acstream.errors import ApiError, ErrorCode

    service, conn, registry = build_service(tmp_path)
    vid, _ = registry.create([("a", b"a")], "utf-8")
    sid = service.open_session(vid)["sid"]
    with pytest.raises(ApiError) as exc:
        service.list_hits_page(sid, cursor=None, limit=10_000)
    assert exc.value.code == ErrorCode.LIMIT_INVALID
