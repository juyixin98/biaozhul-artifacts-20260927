"""版本存储：SQLite 实现 + 内存实现（测试/演示用）。

schema
------
* ``documents``     文档头：head 版本、当前文本、初始文本、裁剪基线
* ``revisions``     每个修订一行：来源、基线、长度、组件 JSON、校验和
* ``submitted_ops`` 幂等表：(doc, client_id, client_op_id) -> 结果修订
* ``snapshots``     裁剪快照：某修订版本的完整文本

线程模型：SQLite 单文件写串行化。每次提交在 ``BEGIN IMMEDIATE``
事务中完成“读历史 -> 变换 -> 写修订”全过程（:class:`service.OTService`
通过 :meth:`Repository.run_transaction` 驱动）。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import ComputationFailed, DocNotFound
from .engine import StoredRevision
from .textmodel import comps_from_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    head_rev      INTEGER NOT NULL,
    head_text     TEXT NOT NULL,
    head_iddoc    TEXT NOT NULL DEFAULT '',
    initial_text  TEXT NOT NULL,
    baseline_rev  INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    doc_id        TEXT NOT NULL,
    rev           INTEGER NOT NULL,
    client_id     TEXT NOT NULL,
    client_op_id  INTEGER NOT NULL,
    base_rev      INTEGER NOT NULL,
    length_before INTEGER NOT NULL,
    length_after  INTEGER NOT NULL,
    checksum      TEXT NOT NULL,
    ops_json      TEXT NOT NULL,
    PRIMARY KEY (doc_id, rev)
);
CREATE TABLE IF NOT EXISTS submitted_ops (
    doc_id        TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    client_op_id  INTEGER NOT NULL,
    result_rev    INTEGER NOT NULL,
    raw_signature TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    PRIMARY KEY (doc_id, client_id, client_op_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    doc_id        TEXT NOT NULL,
    rev           INTEGER NOT NULL,
    text          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (doc_id, rev)
);
"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    doc_id: str
    rev: int
    text: str


def _iddoc_to_json(doc) -> str:
    import json
    return json.dumps({
        "n_base": doc.n_base,
        "base_alive": doc.base_alive,
        "base_ch": doc.base_ch,
        "hang": doc.hang,
    }, ensure_ascii=False)


def _iddoc_from_json(raw: str):
    import json
    from .iddoc import IdDoc
    d = json.loads(raw)
    hang = [[tuple(x) for x in slot] for slot in d["hang"]]
    return IdDoc(d["n_base"], list(d["base_alive"]), list(d["base_ch"]), hang)


def row_to_revision(row: sqlite3.Row | dict) -> StoredRevision:
    ops_json = row["ops_json"]
    return StoredRevision(
        doc_id=row["doc_id"],
        rev=row["rev"],
        client_id=row["client_id"],
        client_op_id=row["client_op_id"],
        base_rev=row["base_rev"],
        ops=comps_from_json(ops_json),
        length_before=row["length_before"],
        length_after=row["length_after"],
        checksum=row["checksum"],
        ops_json=ops_json,
    )


class Repository:
    """存储接口（SQLite 与内存实现共享同一契约）。"""

    # 文档
    def create_document(self, doc_id: str, initial_text: str) -> None: ...
    def exists(self, doc_id: str) -> bool: ...
    def head_rev(self, doc_id: str) -> int: ...
    def head_text(self, doc_id: str) -> str: ...
    def head_iddoc_json(self, doc_id: str) -> str: ...
    def set_head(self, doc_id: str, rev: int, text: str,
                 iddoc_json: str = "") -> None: ...
    def initial_text(self, doc_id: str) -> str: ...
    def baseline_rev(self, doc_id: str) -> int: ...

    # 修订
    def get_revision(self, doc_id: str, rev: int) -> StoredRevision: ...
    def insert_revision(self, revision: StoredRevision) -> None: ...

    # 幂等
    def lookup_submission(self, doc_id: str, client_id: str,
                          client_op_id: int) -> int | None: ...
    def lookup_raw_signature(self, doc_id: str, client_id: str,
                             client_op_id: int) -> str | None: ...
    def remember_submission(self, doc_id: str, client_id: str,
                            client_op_id: int, result_rev: int,
                            raw_signature: str = "") -> None: ...

    # 快照 / 裁剪
    def put_snapshot(self, doc_id: str, rev: int, text: str) -> None: ...
    def latest_snapshot_at_or_before(self, doc_id: str, rev: int) -> Snapshot: ...
    def trim(self, doc_id: str, new_baseline: int) -> None: ...

    # 事务
    def run_transaction(self, fn: Callable[[], object]): ...

    def ops_json_for(self, revision: StoredRevision) -> str:
        """返回校验所用的规范 JSON；默认用修订自带值。"""
        return revision.ops_json


# ---------------------------------------------------------------- SQLite


class SqliteRepository(Repository):
    def __init__(self, path: str | Path):
        self.path = ":memory:" if str(path) == ":memory:" else str(path)
        self._lock = threading.RLock()
        self._mem: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._mem = self._connect()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None：完全手动事务，避免驱动隐式提交与我们的
        # BEGIN IMMEDIATE 冲突。
        conn = sqlite3.connect(self.path, check_same_thread=False,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        return conn

    def run_transaction(self, fn: Callable[[], object]):
        # 进程内串行 + IMMEDIATE，保证“读-算-写”原子
        with self._lock:
            own_conn = self._mem is None
            conn = self._mem or self._connect()
            self._conn = conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn()
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
                    return result
            finally:
                self._conn = None
                if own_conn:
                    conn.close()

    def _c(self) -> sqlite3.Connection:
        c = getattr(self, "_conn", None)
        if c is None:
            # 非事务上下文：短连接（只读/管理调用用）
            if self._mem is not None:
                return self._mem
            return self._connect()
        return c

    def _finish_short(self, conn, commit: bool) -> None:
        # 所有业务写都发生在 run_transaction 内（self._conn 已设置），
        # 提交/回滚由事务统一负责，这里不做任何事。
        return

    # -------------------------------------------------- documents
    def create_document(self, doc_id: str, initial_text: str) -> None:
        import time

        from .iddoc import IdDoc
        iddoc_json = _iddoc_to_json(IdDoc.initial(initial_text))
        conn = self._c()
        now = time.time()
        conn.execute(
            "INSERT INTO documents(doc_id, head_rev, head_text, head_iddoc,"
            " initial_text, baseline_rev, created_at, updated_at)"
            " VALUES (?,?,?,?,?,0,?,?)",
            (doc_id, 0, initial_text, iddoc_json, initial_text, now, now),
        )
        conn.execute(
            "INSERT INTO snapshots(doc_id, rev, text, created_at) VALUES (?,?,?,?)",
            (doc_id, 0, initial_text, now),
        )
        self._finish_short(conn, True)

    def _doc_row(self, conn, doc_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM documents WHERE doc_id=?", (doc_id,)
        ).fetchone()
        if row is None:
            raise DocNotFound(f"文档 {doc_id!r} 不存在", reason="DOC_NOT_FOUND",
                              details={"doc_id": doc_id})
        return row

    def exists(self, doc_id: str) -> bool:
        conn = self._c()
        row = conn.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        return row is not None

    def head_rev(self, doc_id: str) -> int:
        return self._doc_row(self._c(), doc_id)["head_rev"]

    def head_text(self, doc_id: str) -> str:
        return self._doc_row(self._c(), doc_id)["head_text"]

    def head_iddoc_json(self, doc_id: str) -> str:
        return self._doc_row(self._c(), doc_id)["head_iddoc"]

    def initial_text(self, doc_id: str) -> str:
        return self._doc_row(self._c(), doc_id)["initial_text"]

    def baseline_rev(self, doc_id: str) -> int:
        return self._doc_row(self._c(), doc_id)["baseline_rev"]

    def set_head(self, doc_id: str, rev: int, text: str,
                 iddoc_json: str = "") -> None:
        import time

        conn = self._c()
        if iddoc_json:
            conn.execute(
                "UPDATE documents SET head_rev=?, head_text=?, head_iddoc=?,"
                " updated_at=? WHERE doc_id=?",
                (rev, text, iddoc_json, time.time(), doc_id),
            )
        else:
            conn.execute(
                "UPDATE documents SET head_rev=?, head_text=?, updated_at=?"
                " WHERE doc_id=?",
                (rev, text, time.time(), doc_id),
            )
        self._finish_short(conn, True)

    # -------------------------------------------------- revisions
    def get_revision(self, doc_id: str, rev: int) -> StoredRevision:
        conn = self._c()
        row = conn.execute(
            "SELECT * FROM revisions WHERE doc_id=? AND rev=?", (doc_id, rev)
        ).fetchone()
        if row is None:
            raise ComputationFailed(
                f"修订 r{rev} 缺失（历史不完整）",
                reason="MISSING_REVISION",
                details={"doc_id": doc_id, "rev": rev},
            )
        return row_to_revision(row)

    def insert_revision(self, revision: StoredRevision) -> None:
        conn = self._c()
        conn.execute(
            "INSERT INTO revisions(doc_id, rev, client_id, client_op_id, base_rev,"
            " length_before, length_after, checksum, ops_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                revision.doc_id, revision.rev, revision.client_id,
                revision.client_op_id, revision.base_rev, revision.length_before,
                revision.length_after, revision.checksum, revision.ops_json,
            ),
        )
        self._finish_short(conn, True)

    # -------------------------------------------------- idempotency
    def lookup_submission(self, doc_id: str, client_id: str,
                          client_op_id: int) -> int | None:
        conn = self._c()
        row = conn.execute(
            "SELECT result_rev FROM submitted_ops WHERE doc_id=? AND client_id=?"
            " AND client_op_id=?",
            (doc_id, client_id, client_op_id),
        ).fetchone()
        return None if row is None else row["result_rev"]

    def lookup_raw_signature(self, doc_id: str, client_id: str,
                             client_op_id: int) -> str | None:
        conn = self._c()
        row = conn.execute(
            "SELECT raw_signature FROM submitted_ops WHERE doc_id=? AND client_id=?"
            " AND client_op_id=?",
            (doc_id, client_id, client_op_id),
        ).fetchone()
        return None if row is None else row["raw_signature"]

    def remember_submission(self, doc_id: str, client_id: str,
                            client_op_id: int, result_rev: int,
                            raw_signature: str = "") -> None:
        import time

        conn = self._c()
        conn.execute(
            "INSERT INTO submitted_ops(doc_id, client_id, client_op_id, result_rev,"
            " raw_signature, created_at) VALUES (?,?,?,?,?,?)",
            (doc_id, client_id, client_op_id, result_rev,
             raw_signature, time.time()),
        )
        self._finish_short(conn, True)

    # -------------------------------------------------- snapshots
    def put_snapshot(self, doc_id: str, rev: int, text: str) -> None:
        import time

        conn = self._c()
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(doc_id, rev, text, created_at)"
            " VALUES (?,?,?,?)",
            (doc_id, rev, text, time.time()),
        )
        self._finish_short(conn, True)

    def latest_snapshot_at_or_before(self, doc_id: str, rev: int) -> Snapshot:
        conn = self._c()
        row = conn.execute(
            "SELECT doc_id, rev, text FROM snapshots WHERE doc_id=? AND rev<=?"
            " ORDER BY rev DESC LIMIT 1",
            (doc_id, rev),
        ).fetchone()
        if row is None:
            # 文档创建时一定写入 rev=0 快照；没有说明库损坏
            raise ComputationFailed("缺少基础快照", reason="MISSING_SNAPSHOT")
        return Snapshot(row["doc_id"], row["rev"], row["text"])

    def trim(self, doc_id: str, new_baseline: int) -> None:
        conn = self._c()
        conn.execute(
            "UPDATE documents SET baseline_rev=? WHERE doc_id=?",
            (new_baseline, doc_id),
        )
        conn.execute(
            "DELETE FROM revisions WHERE doc_id=? AND rev<=?",
            (doc_id, new_baseline),
        )
        conn.execute(
            "DELETE FROM snapshots WHERE doc_id=? AND rev<?",
            (doc_id, new_baseline),
        )
        self._finish_short(conn, True)


# ---------------------------------------------------------------- memory


class MemoryRepository(Repository):
    """与 SQLite 同契约的内存实现：纯算法测试无需文件 IO。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._docs: dict[str, dict] = {}
        self._revs: dict[tuple[str, int], StoredRevision] = {}
        self._subs: dict[tuple[str, str, int], tuple[int, str]] = {}
        self._snaps: dict[tuple[str, int], str] = {}

    def run_transaction(self, fn):
        with self._lock:
            return fn()

    def create_document(self, doc_id, initial_text):
        if doc_id in self._docs:
            raise ComputationFailed("文档已存在", reason="DOC_EXISTS")
        from .iddoc import IdDoc
        self._docs[doc_id] = {
            "head_rev": 0, "head_text": initial_text,
            "head_iddoc": _iddoc_to_json(IdDoc.initial(initial_text)),
            "initial_text": initial_text, "baseline_rev": 0,
        }
        self._snaps[(doc_id, 0)] = initial_text

    def exists(self, doc_id):
        return doc_id in self._docs

    def _d(self, doc_id):
        if doc_id not in self._docs:
            raise DocNotFound(f"文档 {doc_id!r} 不存在", details={"doc_id": doc_id})
        return self._docs[doc_id]

    def head_rev(self, doc_id):
        return self._d(doc_id)["head_rev"]

    def head_text(self, doc_id):
        return self._d(doc_id)["head_text"]

    def head_iddoc_json(self, doc_id):
        return self._d(doc_id)["head_iddoc"]

    def initial_text(self, doc_id):
        return self._d(doc_id)["initial_text"]

    def baseline_rev(self, doc_id):
        return self._d(doc_id)["baseline_rev"]

    def set_head(self, doc_id, rev, text, iddoc_json=""):
        d = self._d(doc_id)
        d["head_rev"] = rev
        d["head_text"] = text
        if iddoc_json:
            d["head_iddoc"] = iddoc_json

    def get_revision(self, doc_id, rev):
        key = (doc_id, rev)
        if key not in self._revs:
            raise ComputationFailed(
                f"修订 r{rev} 缺失（历史不完整）",
                reason="MISSING_REVISION",
                details={"doc_id": doc_id, "rev": rev},
            )
        return self._revs[key]

    def insert_revision(self, revision):
        self._revs[(revision.doc_id, revision.rev)] = revision

    def lookup_submission(self, doc_id, client_id, client_op_id):
        v = self._subs.get((doc_id, client_id, client_op_id))
        return None if v is None else v[0]

    def lookup_raw_signature(self, doc_id, client_id, client_op_id):
        v = self._subs.get((doc_id, client_id, client_op_id))
        return None if v is None else v[1]

    def remember_submission(self, doc_id, client_id, client_op_id, result_rev,
                            raw_signature=""):
        self._subs[(doc_id, client_id, client_op_id)] = (result_rev, raw_signature)

    def put_snapshot(self, doc_id, rev, text):
        self._snaps[(doc_id, rev)] = text

    def latest_snapshot_at_or_before(self, doc_id, rev):
        cands = [r for (d, r) in self._snaps if d == doc_id and r <= rev]
        if not cands:
            raise ComputationFailed("缺少基础快照", reason="MISSING_SNAPSHOT")
        r = max(cands)
        return Snapshot(doc_id, r, self._snaps[(doc_id, r)])

    def trim(self, doc_id, new_baseline):
        d = self._d(doc_id)
        d["baseline_rev"] = new_baseline
        for key in [k for k in self._revs if k[0] == doc_id and k[1] <= new_baseline]:
            del self._revs[key]
        for key in [k for k in self._snaps if k[0] == doc_id and k[1] < new_baseline]:
            del self._snaps[key]
