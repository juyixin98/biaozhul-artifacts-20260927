"""SQLite 版本存储与持久快照。

- 主库是普通的 SQLite 文件（WAL 模式），存词条与 meta（规范化版本、修订号等）。
- 快照用 SQLite 在线备份 API（``Connection.backup``）拷贝出**独立的 .db 文件**，
  存放在快照目录；恢复即把快照文件备份回主库后重建内存索引。
- 打开主库时校验 ``normalizer_version``：与当前二进制不一致时抛
  ``NormalizerVersionMismatch``，拒绝在旧索引上查询——不会静默错误。
- 所有 SQL 均参数化；写操作在事务内批量提交。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .errors import (
    NormalizerVersionMismatch,
    SnapshotConflict,
    SnapshotNotFound,
)
from .normalizer import NORMALIZER_VERSION
from .trie import Entry

META_KEY_NORMALIZER = "normalizer_version"
META_KEY_REVISION = "revision"
META_KEY_CREATED_AT = "created_at"
META_KEY_UPDATED_AT = "updated_at"
META_KEY_SNAPSHOTS = "snapshots"  # 快照登记表（JSON）

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id      TEXT PRIMARY KEY,
    surface TEXT NOT NULL,
    key     TEXT NOT NULL,
    score   INTEGER NOT NULL CHECK (score >= 0)
);
CREATE INDEX IF NOT EXISTS idx_entries_key ON entries(key);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class SqliteStore:
    def __init__(self, db_path: Path, snapshot_dir: Path) -> None:
        self.db_path = db_path
        self.snapshot_dir = snapshot_dir
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.conn = _connect(self.db_path)
        self.conn.executescript(_DDL)
        self._bootstrap_meta()
        self._assert_version()

    # ---- meta ------------------------------------------------------------

    def _bootstrap_meta(self) -> None:
        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (META_KEY_NORMALIZER, NORMALIZER_VERSION),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (META_KEY_REVISION, "0"),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (META_KEY_CREATED_AT, now),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (META_KEY_SNAPSHOTS, "[]"),
        )
        self.conn.commit()

    def _assert_version(self) -> None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (META_KEY_NORMALIZER,)
        ).fetchone()
        stored = row["value"] if row else None
        if stored != NORMALIZER_VERSION:
            raise NormalizerVersionMismatch(
                f"数据库使用规范化版本 {stored!r}，当前程序为 {NORMALIZER_VERSION!r}；"
                "索引键语义已变化，必须用新版本重建（删除 db 或走新快照恢复）。",
                details={"stored": stored, "current": NORMALIZER_VERSION},
            )

    def normalizer_version(self) -> str:
        return NORMALIZER_VERSION

    def revision(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (META_KEY_REVISION,)
        ).fetchone()
        return int(row["value"])

    def _bump_revision(self) -> int:
        rev = self.revision() + 1
        self.conn.execute(
            "UPDATE meta SET value=? WHERE key=?", (str(rev), META_KEY_REVISION)
        )
        self.conn.execute(
            "UPDATE meta SET value=? WHERE key=?", (_now(), META_KEY_UPDATED_AT)
        )
        return rev

    def meta_snapshot(self) -> dict[str, object]:
        rows = self.conn.execute("SELECT key, value FROM meta").fetchall()
        meta = {r["key"]: r["value"] for r in rows}
        meta[META_KEY_REVISION] = int(meta.get(META_KEY_REVISION, 0))
        return meta  # type: ignore[return-value]

    # ---- entries ---------------------------------------------------------

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"])

    def load_all(self) -> list[Entry]:
        rows = self.conn.execute(
            "SELECT id, surface, key, score FROM entries ORDER BY id"
        ).fetchall()
        return [
            Entry(id=r["id"], surface=r["surface"], key=r["key"], score=int(r["score"]))
            for r in rows
        ]

    def get(self, entry_id: str) -> Entry | None:
        r = self.conn.execute(
            "SELECT id, surface, key, score FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        if r is None:
            return None
        return Entry(id=r["id"], surface=r["surface"], key=r["key"], score=int(r["score"]))

    def upsert_many(self, entries: Iterable[Entry]) -> int:
        entries = list(entries)
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO entries(id, surface, key, score)
                VALUES (:id, :surface, :key, :score)
                ON CONFLICT(id) DO UPDATE SET
                    surface=excluded.surface,
                    key=excluded.key,
                    score=excluded.score
                """,
                [
                    {"id": e.id, "surface": e.surface, "key": e.key, "score": e.score}
                    for e in entries
                ],
            )
            self._bump_revision()
        return len(entries)

    def delete(self, entry_id: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
            self._bump_revision()
            return cur.rowcount > 0

    def set_score(self, entry_id: str, score: int) -> Entry | None:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE entries SET score=? WHERE id=?", (score, entry_id)
            )
            if cur.rowcount == 0:
                return None
            self._bump_revision()
        return self.get(entry_id)

    # ---- 持久快照 --------------------------------------------------------

    def _snapshot_registry(self) -> list[dict[str, object]]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (META_KEY_SNAPSHOTS,)
        ).fetchone()
        return json.loads(row["value"]) if row else []

    def _write_snapshot_registry(self, items: list[dict[str, object]]) -> None:
        self.conn.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (json.dumps(items, ensure_ascii=False, sort_keys=True), META_KEY_SNAPSHOTS),
        )

    def create_snapshot(self, name: str, note: str = "") -> dict[str, object]:
        safe = _safe_name(name)
        existing = self._snapshot_registry()
        if any(s["name"] == safe for s in existing):
            raise SnapshotConflict(
                f"快照 {safe!r} 已存在", details={"name": safe}
            )
        path = self.snapshot_dir / f"{safe}.db"
        # 在线备份：把当前主库完整复制成独立文件（词条 + meta 一并拷入）。
        backup_conn = _connect(path)
        try:
            self.conn.backup(backup_conn)
            # 恢复时必须能通过版本校验：备份库里已是同一版本，无需特殊处理。
            backup_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            backup_conn.close()
        record = {
            "name": safe,
            "file": path.name,
            "revision": self.revision(),
            "normalizer_version": NORMALIZER_VERSION,
            "entry_count": self.count(),
            "created_at": _now(),
            "note": note,
        }
        with self.conn:
            existing.append(record)
            self._write_snapshot_registry(existing)
        return record

    def list_snapshots(self) -> list[dict[str, object]]:
        return self._snapshot_registry()

    def restore_snapshot(self, name: str) -> dict[str, object]:
        safe = _safe_name(name)
        records = self._snapshot_registry()
        record = next((s for s in records if s["name"] == safe), None)
        path = self.snapshot_dir / f"{safe}.db"
        if record is None or not path.exists():
            raise SnapshotNotFound(
                f"快照 {safe!r} 不存在或文件缺失",
                details={"name": safe, "expected_file": str(path)},
            )
        # 从快照文件备份回主库（覆盖）。用独立连接读快照，避免与自身互拷。
        src = _connect(path)
        try:
            src.row_factory = sqlite3.Row
            ver_row = src.execute(
                "SELECT value FROM meta WHERE key=?", (META_KEY_NORMALIZER,)
            ).fetchone()
            snap_ver = ver_row["value"] if ver_row else None
            if snap_ver != NORMALIZER_VERSION:
                raise NormalizerVersionMismatch(
                    f"快照 {safe!r} 的规范化版本 {snap_ver!r} 与当前 {NORMALIZER_VERSION!r} 不一致，拒绝恢复",
                    details={"snapshot": snap_ver, "current": NORMALIZER_VERSION},
                )
            with self.conn:
                src.backup(self.conn)
        finally:
            src.close()
        # backup 会覆盖主库内容；重新确保版本一致并返回记录。
        self._assert_version()
        return record

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.conn.close()


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise SnapshotNotFound("快照名不能为空", details={"name": name})
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    bad = [c for c in name if c not in allowed]
    if bad or ".." in name or name.startswith("."):
        raise SnapshotConflict(
            "快照名只允许字母数字与 '-_.'，且不得为路径形式",
            details={"name": name, "rejected_chars": bad},
        )
    return name


def _now() -> str:
    # 统一 UTC ISO8601，含秒；测试日志可据此排序。
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
