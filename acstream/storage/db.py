"""SQLite 存储层。

只负责持久化与行级读写，不含算法逻辑。表设计：

- pattern_versions : 模式集合版本（不可变，指纹唯一）
- patterns         : 版本内的模式（pattern_id 与原始字节）
- sessions         : 流式会话（当前自动机版本、节点号、偏移、状态）
- hits             : 命中流水，按 (session, end, start, pattern_id, seq) 有序
- diagnostic_events: 接受/拒绝/无法判定的决策记录
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pattern_versions (
    version_id     TEXT PRIMARY KEY,
    fingerprint    TEXT NOT NULL UNIQUE,
    pattern_count  INTEGER NOT NULL,
    encoding       TEXT NOT NULL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS patterns (
    version_id   TEXT NOT NULL REFERENCES pattern_versions(version_id),
    pattern_id   TEXT NOT NULL,
    data         BLOB NOT NULL,
    ord          INTEGER NOT NULL,
    PRIMARY KEY (version_id, pattern_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    sid            TEXT PRIMARY KEY,
    version_id     TEXT NOT NULL REFERENCES pattern_versions(version_id),
    fingerprint    TEXT NOT NULL,
    node_state     INTEGER NOT NULL,
    byte_offset    INTEGER NOT NULL,
    feed_count     INTEGER NOT NULL DEFAULT 0,  -- 已喂入块数（含空块），用于稳定排序与诊断
    status         TEXT NOT NULL CHECK (status IN ('open','finished')),
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sid          TEXT NOT NULL REFERENCES sessions(sid),
    end_offset   INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    pattern_id   TEXT NOT NULL,
    feed_seq     INTEGER NOT NULL,          -- 会话内第几次 feed（从 1 起）
    ord_in_feed  INTEGER NOT NULL,          -- feed 内序号
    created_at   REAL NOT NULL,
    UNIQUE (sid, end_offset, start_offset, pattern_id, feed_seq)
);

CREATE INDEX IF NOT EXISTS idx_hits_keyset
    ON hits (sid, end_offset, start_offset, pattern_id, feed_seq);

CREATE TABLE IF NOT EXISTS diagnostic_events (
    event_id     TEXT PRIMARY KEY,
    request_id   TEXT,
    sid          TEXT,
    version_id   TEXT,
    outcome      TEXT NOT NULL CHECK (outcome IN ('accept','reject','undetermined')),
    code         TEXT NOT NULL,
    message      TEXT NOT NULL,
    key_state    TEXT NOT NULL,              -- JSON：脱敏后的关键状态
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diag_sid ON diagnostic_events(sid);
CREATE INDEX IF NOT EXISTS idx_diag_req ON diagnostic_events(request_id);
CREATE INDEX IF NOT EXISTS idx_diag_created ON diagnostic_events(created_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    # FastAPI 同步端点在线程池中执行；本地单进程服务用 check_same_thread=False
    # 配合应用层 RLock 串行化服务调用（见 app.py），保证跨线程使用安全。
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def now_ts() -> float:
    return time.time()


# ----------------------------------------------------------------- repositories


class VersionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def exists_by_fingerprint(self, fingerprint: str) -> str | None:
        row = self._c.execute(
            "SELECT version_id FROM pattern_versions WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        return row[0] if row else None

    def create(
        self,
        version_id: str,
        fingerprint: str,
        encoding: str,
        patterns: list[tuple[str, bytes]],
    ) -> None:
        ts = now_ts()
        self._c.execute(
            "INSERT INTO pattern_versions "
            "(version_id, fingerprint, pattern_count, encoding, created_at) "
            "VALUES (?,?,?,?,?)",
            (version_id, fingerprint, len(patterns), encoding, ts),
        )
        self._c.executemany(
            "INSERT INTO patterns (version_id, pattern_id, data, ord) VALUES (?,?,?,?)",
            [
                (version_id, pid, data, idx)
                for idx, (pid, data) in enumerate(patterns)
            ],
        )

    def get(self, version_id: str) -> sqlite3.Row | None:
        return self._c.execute(
            "SELECT * FROM pattern_versions WHERE version_id = ?", (version_id,)
        ).fetchone()

    def list_patterns(self, version_id: str) -> list[tuple[str, bytes]]:
        rows = self._c.execute(
            "SELECT pattern_id, data FROM patterns WHERE version_id = ? ORDER BY ord",
            (version_id,),
        ).fetchall()
        return [(r["pattern_id"], bytes(r["data"])) for r in rows]

    def list_versions(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self._c.execute(
                "SELECT * FROM pattern_versions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )


class SessionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def create(
        self, sid: str, version_id: str, fingerprint: str
    ) -> None:
        ts = now_ts()
        self._c.execute(
            "INSERT INTO sessions "
            "(sid, version_id, fingerprint, node_state, byte_offset, feed_count, "
            "status, created_at, updated_at) VALUES (?,?,?,0,0,0,'open',?,?)",
            (sid, version_id, fingerprint, ts, ts),
        )

    def get(self, sid: str) -> sqlite3.Row | None:
        return self._c.execute(
            "SELECT * FROM sessions WHERE sid = ?", (sid,)
        ).fetchone()

    def switch_version(
        self,
        sid: str,
        *,
        version_id: str,
        fingerprint: str,
        node_state: int = 0,
    ) -> None:
        """显式版本边界：版本/指纹更新，节点重置为根；偏移与 feed_count 保留。"""
        self._c.execute(
            "UPDATE sessions SET version_id=?, fingerprint=?, node_state=?, "
            "updated_at=? WHERE sid=?",
            (version_id, fingerprint, node_state, now_ts(), sid),
        )

    def advance_feed(
        self, sid: str, *, node_state: int, byte_offset: int
    ) -> None:
        self._c.execute(
            "UPDATE sessions SET node_state=?, byte_offset=?, "
            "feed_count=feed_count+1, updated_at=? WHERE sid=?",
            (node_state, byte_offset, now_ts(), sid),
        )

    def finish(self, sid: str) -> None:
        self._c.execute(
            "UPDATE sessions SET status='finished', updated_at=? WHERE sid=?",
            (now_ts(), sid),
        )


class HitStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def add_many(
        self,
        sid: str,
        feed_seq: int,
        hits: list[tuple[int, int, str]],  # (end, start, pattern_id)
    ) -> None:
        ts = now_ts()
        self._c.executemany(
            "INSERT INTO hits "
            "(sid, end_offset, start_offset, pattern_id, feed_seq, ord_in_feed, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            [
                (sid, end, start, pid, feed_seq, ord, ts)
                for ord, (end, start, pid) in enumerate(hits)
            ],
        )

    def count(self, sid: str) -> int:
        return self._c.execute(
            "SELECT COUNT(*) AS n FROM hits WHERE sid=?", (sid,)
        ).fetchone()["n"]

    def page(
        self,
        sid: str,
        *,
        limit: int,
        after: tuple[int, int, str, int] | None = None,
    ) -> list[sqlite3.Row]:
        """keyset 分页。

        排序键固定为 (end_offset ASC, start_offset DESC, pattern_id ASC,
        feed_seq ASC, ord_in_feed ASC)：同一终止位置上，更长的模式（起点更早）
        排在前面；feed_seq/ord 仅做最终去重决胜。
        """
        if after is None:
            sql = (
                "SELECT * FROM hits WHERE sid=? "
                "ORDER BY end_offset ASC, start_offset DESC, pattern_id ASC, "
                "feed_seq ASC, ord_in_feed ASC LIMIT ?"
            )
            params: tuple[Any, ...] = (sid, limit)
        else:
            end, start, pid, feed_seq = after
            sql = (
                "SELECT * FROM hits WHERE sid=? "
                "AND (end_offset, -start_offset, pattern_id, feed_seq) > (?,?,?,?) "
                "ORDER BY end_offset ASC, start_offset DESC, pattern_id ASC, "
                "feed_seq ASC, ord_in_feed ASC LIMIT ?"
            )
            params = (sid, end, -start, pid, feed_seq, limit)
        return list(self._c.execute(sql, params).fetchall())


class DiagnosticStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def add(
        self,
        *,
        event_id: str,
        request_id: str | None,
        sid: str | None,
        version_id: str | None,
        outcome: str,
        code: str,
        message: str,
        key_state: dict,
    ) -> None:
        self._c.execute(
            "INSERT INTO diagnostic_events "
            "(event_id, request_id, sid, version_id, outcome, code, message, "
            "key_state, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                request_id,
                sid,
                version_id,
                outcome,
                code,
                message,
                json.dumps(key_state, ensure_ascii=False, sort_keys=True),
                now_ts(),
            ),
        )

    def list_events(
        self,
        *,
        sid: str | None = None,
        request_id: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[Any] = []
        if sid:
            where.append("sid=?")
            params.append(sid)
        if request_id:
            where.append("request_id=?")
            params.append(request_id)
        if outcome:
            where.append("outcome=?")
            params.append(outcome)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, offset])
        return list(
            self._c.execute(
                f"SELECT * FROM diagnostic_events {clause} "
                "ORDER BY created_at ASC, rowid ASC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        )

    def count_events(
        self,
        *,
        sid: str | None = None,
        request_id: str | None = None,
        outcome: str | None = None,
    ) -> int:
        where: list[str] = []
        params: list[Any] = []
        if sid:
            where.append("sid=?")
            params.append(sid)
        if request_id:
            where.append("request_id=?")
            params.append(request_id)
        if outcome:
            where.append("outcome=?")
            params.append(outcome)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return self._c.execute(
            f"SELECT COUNT(*) AS n FROM diagnostic_events {clause}", params
        ).fetchone()["n"]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
