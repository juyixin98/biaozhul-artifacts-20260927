"""持久化与重建：进程重启后自动机从 SQLite 重建，指纹与命中结果不变。"""

from __future__ import annotations

from acstream.diagnostics import Diagnostics
from acstream.services.matcher import MatcherService
from acstream.services.version_registry import VersionRegistry
from acstream.storage.db import DiagnosticStore, connect, init_db

SECRET = b"unit-test-secret-key-0123456789ab"


def test_rebuild_automaton_after_restart(tmp_path) -> None:
    db = tmp_path / "persist.db"
    conn1 = connect(db)
    init_db(conn1)
    reg1 = VersionRegistry(conn1)
    diag1 = Diagnostics(DiagnosticStore(conn1))
    svc1 = MatcherService(conn1, reg1, diag1, cursor_secret=SECRET)
    vid, auto1 = reg1.create(
        [("he", b"he"), ("she", b"she"), ("hers", b"hers")], "utf-8"
    )
    sid = svc1.open_session(vid)["sid"]
    svc1.feed(sid, b"ushe", expected_offset=None,
              expected_fingerprint=None, finish=False)
    conn1.commit()
    conn1.close()

    # 模拟重启：新连接、空内存缓存。
    conn2 = connect(db)
    init_db(conn2)
    reg2 = VersionRegistry(conn2)
    auto2 = reg2.get(vid)  # 触发从库重建
    assert auto2.fingerprint == auto1.fingerprint

    diag2 = Diagnostics(DiagnosticStore(conn2))
    svc2 = MatcherService(conn2, reg2, diag2, cursor_secret=SECRET)
    # 会话节点状态/偏移也从库恢复，继续喂剩余部分。
    state = svc2.get_state(sid)
    assert state["state"]["byte_offset"] == 4
    svc2.feed(sid, b"rs", expected_offset=4,
              expected_fingerprint=None, finish=True)
    page = svc2.list_hits_page(sid, cursor=None, limit=100)
    hits = sorted((i["pattern_id"], i["start"], i["end"]) for i in page["items"])
    assert hits == [("he", 2, 4), ("hers", 2, 6), ("she", 1, 4)]


def test_cursor_survives_restart_with_persisted_key(tmp_path, monkeypatch) -> None:
    # 不提供 AC_CURSOR_SECRET：密钥落本地文件，重启后游标仍可验签。
    monkeypatch.delenv("AC_CURSOR_SECRET", raising=False)
    from acstream.config import Settings

    db = tmp_path / "k.db"
    monkeypatch.setenv("AC_DB_PATH", str(db))
    settings = Settings.load()

    conn1 = connect(db)
    init_db(conn1)
    reg1 = VersionRegistry(conn1)
    diag = Diagnostics(DiagnosticStore(conn1))
    svc1 = MatcherService(conn1, reg1, diag, cursor_secret=settings.cursor_secret)
    vid, _ = reg1.create([("a", b"a")], "utf-8")
    sid = svc1.open_session(vid)["sid"]
    svc1.feed(sid, b"aa", expected_offset=None,
              expected_fingerprint=None, finish=False)
    page1 = svc1.list_hits_page(sid, cursor=None, limit=1)
    token = page1["next_cursor"]
    conn1.close()

    settings2 = Settings.load()  # 重新读取持久化密钥
    conn2 = connect(db)
    init_db(conn2)
    reg2 = VersionRegistry(conn2)
    diag2 = Diagnostics(DiagnosticStore(conn2))
    svc2 = MatcherService(conn2, reg2, diag2, cursor_secret=settings2.cursor_secret)
    page2 = svc2.list_hits_page(sid, cursor=token, limit=10)
    assert len(page2["items"]) == 1
