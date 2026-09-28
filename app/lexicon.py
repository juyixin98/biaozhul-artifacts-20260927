"""SQLite 版本化词典存储。

每个不可变版本（version）保存一份完整词典快照；同一时刻至多一个激活版本。
写操作仅在创建/激活版本时发生，读路径纯 SELECT。
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id  TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    entry_count INTEGER NOT NULL,
    source      TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0,
    checksum    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    version_id TEXT NOT NULL,
    word       TEXT NOT NULL,
    freq       INTEGER NOT NULL,
    PRIMARY KEY (version_id, word)
);
CREATE INDEX IF NOT EXISTS idx_entries_version ON entries(version_id);
"""


class VersionError(Exception):
    pass


class VersionNotFound(VersionError):
    pass


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _checksum(words: Sequence[tuple[str, int]]) -> str:
    import hashlib

    h = hashlib.sha256()
    for word, freq in words:
        h.update(word.encode("utf-8"))
        h.update(b"\0")
        h.update(str(int(freq)).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def create_version(
    conn: sqlite3.Connection,
    entries: Iterable[tuple[str, int]],
    *,
    version_id: Optional[str] = None,
    source: str = "unknown",
    activate: bool = True,
) -> str:
    """创建不可变词典版本。重复单词取较大频次；空词典拒绝创建。"""
    merged: dict[str, int] = {}
    for word, freq in entries:
        word = word.strip()
        if not word:
            continue
        freq = int(freq)
        if freq < 0:
            raise VersionError(f"词条频次不能为负: {word!r}")
        merged[word] = max(merged.get(word, 0), freq)

    if not merged:
        raise VersionError("拒绝创建空词典版本")

    ordered = sorted(merged.items(), key=lambda kv: kv[0])
    vid = version_id or f"v{int(time.time() * 1000)}"
    checksum = _checksum(ordered)

    with transaction(conn):
        if conn.execute(
            "SELECT 1 FROM versions WHERE version_id = ?", (vid,)
        ).fetchone():
            raise VersionError(f"版本已存在: {vid}")
        conn.executemany(
            "INSERT INTO entries(version_id, word, freq) VALUES (?, ?, ?)",
            [(vid, w, f) for w, f in ordered],
        )
        conn.execute(
            "INSERT INTO versions(version_id, created_at, entry_count, source, is_active, checksum)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (vid, time.time(), len(ordered), source, 1 if activate else 0, checksum),
        )
        if activate:
            conn.execute("UPDATE versions SET is_active = 0")
            conn.execute(
                "UPDATE versions SET is_active = 1 WHERE version_id = ?", (vid,)
            )
    return vid


def activate_version(conn: sqlite3.Connection, version_id: str) -> None:
    with transaction(conn):
        row = conn.execute(
            "SELECT 1 FROM versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise VersionNotFound(f"版本不存在: {version_id}")
        conn.execute("UPDATE versions SET is_active = 0")
        conn.execute("UPDATE versions SET is_active = 1 WHERE version_id = ?", (version_id,))


def get_active_version(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT version_id FROM versions WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["version_id"] if row else None


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT version_id, created_at, entry_count, source, is_active, checksum"
        " FROM versions ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_entries(conn: sqlite3.Connection, version_id: str) -> list[tuple[str, int]]:
    row = conn.execute(
        "SELECT 1 FROM versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise VersionNotFound(f"版本不存在: {version_id}")
    rows = conn.execute(
        "SELECT word, freq FROM entries WHERE version_id = ? ORDER BY word",
        (version_id,),
    ).fetchall()
    return [(r["word"], r["freq"]) for r in rows]


def load_jsonl(path: str | Path) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            try:
                out.append((obj["word"], int(obj.get("freq", 0))))
            except KeyError as exc:
                raise ValueError(f"{path}:{lineno} 缺少 word 字段") from exc
    return out
