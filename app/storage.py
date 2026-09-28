"""版本存储：SQLite 持久化（文档、操作历史、幂等、快照、裁剪水位）。

存储层只负责数据读写与事务；OT 算法与错误编排由 :mod:`app.service` 负责。
单进程使用：WAL 模式 + 一把连接，所有写入在服务层锁保护下进行。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .errors import StorageFailure
from .models import Op


@dataclass(frozen=True, slots=True)
class StoredOp:
    doc_id: str
    revision: int          # 该操作提交后文档的版本号（1 起递增）
    client_id: str
    client_seq: int
    base_revision: int     # 客户端提交时声明的基线版本
    op: Op                 # 落库形态（必要时已 transform 到 head）
    idem_key: str | None
    created_at: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    head_revision   INTEGER NOT NULL,
    pruned_horizon  INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    doc_id          TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    client_id       TEXT NOT NULL,
    client_seq      INTEGER NOT NULL,
    base_revision   INTEGER NOT NULL,
    op_json         TEXT NOT NULL,
    base_len        INTEGER NOT NULL,
    target_len      INTEGER NOT NULL,
    idem_key        TEXT,
    created_at      REAL NOT NULL,
    PRIMARY KEY (doc_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_ops_doc_rev ON operations (doc_id, revision);
CREATE TABLE IF NOT EXISTS idempotency (
    doc_id          TEXT NOT NULL,
    idem_key        TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    request_sha     TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (doc_id, idem_key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    doc_id          TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (doc_id, revision)
);
"""


class Storage:
    def __init__(self, db_path: str = ":memory:"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # -------------------------------------------------------------- 文档
    def create_document(self, doc_id: str, text: str) -> None:
        now = time.time()
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO documents(doc_id, text, head_revision, pruned_horizon, created_at)"
                    " VALUES (?,?,?,0,?)",
                    (doc_id, text, 0, now),
                )
                # 0 号快照：空历史下的重建基线
                self._conn.execute(
                    "INSERT INTO snapshots(doc_id, revision, text, created_at) VALUES (?,?,?,?)",
                    (doc_id, 0, text, now),
                )
        except sqlite3.IntegrityError as e:
            raise StorageFailure(f"document already exists: {doc_id}", details={"cause": str(e)})

    def get_document_row(self, doc_id: str) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM documents WHERE doc_id=?", (doc_id,)
        )
        return cur.fetchone()

    def list_documents(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT doc_id, head_revision, pruned_horizon, length(text) AS char_len, created_at"
            " FROM documents ORDER BY created_at"
        ))

    # -------------------------------------------------------------- 历史
    def get_ops(self, doc_id: str, after: int, limit: int) -> list[StoredOp]:
        cur = self._conn.execute(
            "SELECT * FROM operations WHERE doc_id=? AND revision>? ORDER BY revision ASC LIMIT ?",
            (doc_id, after, limit),
        )
        return [self._row_to_stored(r) for r in cur.fetchall()]

    def get_op(self, doc_id: str, revision: int) -> StoredOp | None:
        cur = self._conn.execute(
            "SELECT * FROM operations WHERE doc_id=? AND revision=?", (doc_id, revision)
        )
        row = cur.fetchone()
        return self._row_to_stored(row) if row else None

    def count_ops(self, doc_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM operations WHERE doc_id=?", (doc_id,)
        )
        return cur.fetchone()["n"]

    @staticmethod
    def _row_to_stored(row: sqlite3.Row) -> StoredOp:
        return StoredOp(
            doc_id=row["doc_id"],
            revision=row["revision"],
            client_id=row["client_id"],
            client_seq=row["client_seq"],
            base_revision=row["base_revision"],
            op=Op.from_dict(json.loads(row["op_json"])),
            idem_key=row["idem_key"],
            created_at=row["created_at"],
        )

    def get_client_seq_op(self, doc_id: str, client_id: str, seq: int) -> StoredOp | None:
        cur = self._conn.execute(
            "SELECT * FROM operations WHERE doc_id=? AND client_id=? AND client_seq=?",
            (doc_id, client_id, seq),
        )
        row = cur.fetchone()
        return self._row_to_stored(row) if row else None

    # ------------------------------------------------------------ 幂等键
    def get_idem(self, doc_id: str, key: str) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM idempotency WHERE doc_id=? AND idem_key=?", (doc_id, key)
        )
        return cur.fetchone()

    # -------------------------------------------------------------- 快照
    def get_snapshot(self, doc_id: str, revision: int) -> str | None:
        cur = self._conn.execute(
            "SELECT text FROM snapshots WHERE doc_id=? AND revision=?", (doc_id, revision)
        )
        row = cur.fetchone()
        return row["text"] if row else None

    def latest_snapshot_revision(self, doc_id: str) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS r FROM snapshots WHERE doc_id=?",
            (doc_id,),
        )
        return cur.fetchone()["r"]

    def rebuild_text(self, doc_id: str, target_revision: int) -> str:
        """从最近快照前向重放，构造 ``target_revision`` 版本的全文。"""
        row = self.get_document_row(doc_id)
        if row is None:
            raise StorageFailure(f"document missing during rebuild: {doc_id}")
        snap_rev = self.latest_snapshot_revision(doc_id)
        if snap_rev > target_revision:  # 理论不可达：裁剪后只保留水位快照
            raise StorageFailure("snapshot ahead of requested revision")
        text = self.get_snapshot(doc_id, snap_rev)
        for sop in self.get_ops(doc_id, snap_rev, target_revision - snap_rev):
            from .ot import apply  # 延迟导入避免循环
            text = apply(sop.op, text)
        return text

    # ---------------------------------------------------------- 原子提交
    def commit_revision(
        self,
        doc_id: str,
        stored: StoredOp,
        new_text: str,
        idem_key: str | None,
        request_sha: str | None,
    ) -> None:
        """一次事务内：追加操作、推进 head、（可选）登记幂等键。"""
        now = stored.created_at
        with self._conn:
            self._conn.execute(
                "UPDATE documents SET text=?, head_revision=? WHERE doc_id=?",
                (new_text, stored.revision, doc_id),
            )
            self._conn.execute(
                "INSERT INTO operations(doc_id, revision, client_id, client_seq,"
                " base_revision, op_json, base_len, target_len, idem_key, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    doc_id,
                    stored.revision,
                    stored.client_id,
                    stored.client_seq,
                    stored.base_revision,
                    json.dumps(stored.op.to_dict(), ensure_ascii=False),
                    stored.op.base_len,
                    stored.op.target_len,
                    idem_key,
                    now,
                ),
            )
            if idem_key is not None:
                self._conn.execute(
                    "INSERT INTO idempotency(doc_id, idem_key, revision, request_sha, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (doc_id, idem_key, stored.revision, request_sha, now),
                )

    def prune(self, doc_id: str, new_horizon: int, snapshot_text: str) -> None:
        """裁剪到 ``new_horizon``：写水位快照、删旧操作与旧快照、推进水位。"""
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO snapshots(doc_id, revision, text, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(doc_id, revision) DO UPDATE SET text=excluded.text",
                (doc_id, new_horizon, snapshot_text, now),
            )
            self._conn.execute(
                "DELETE FROM operations WHERE doc_id=? AND revision<=?",
                (doc_id, new_horizon),
            )
            self._conn.execute(
                "DELETE FROM snapshots WHERE doc_id=? AND revision<>?",
                (doc_id, new_horizon),
            )
            self._conn.execute(
                "UPDATE documents SET pruned_horizon=? WHERE doc_id=?",
                (new_horizon, doc_id),
            )
