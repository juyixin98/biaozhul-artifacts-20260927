"""加密审计存储（SQLite + Fernet）。

落库内容：
- requests：请求身份、规则档快照、引擎版本、处理长度、状态、错误分类、
  处理时间戳；不含原文。
- mappings：每条替换的位置映射；原文以 Fernet (AES-128-CBC + HMAC)
  加密后存储，库内不留明文；另存原文 sha256 供完整性校验。
- uncertainties：失败原因与不确定结论（不含原文片段，只存分类码/区间/
  说明）。
- events：关键处理步骤（CHUNK_RECEIVED / OVERLAP_REJECTED /
  RESIDUAL_FOUND / FINALIZED 等），只存结构化步骤，不存原文。

设计要点：
- 进程内每个 store 一把串行写锁 + WAL，保证测试并发与会话隔离；
- 原文只在显式带审计令牌的接口中解密返回；
- 任何写库异常都转换为 AuditError，不吞异常、不泄漏细节到普通日志。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken

from ..core.redactor import (
    MappingRecord,
    RedactionResult,
    Uncertainty,
)


class AuditError(RuntimeError):
    """审计存储不可用或数据损坏。"""


@dataclass(frozen=True)
class StoredMapping:
    request_id: str
    index: int
    rule_id: str
    source: str
    key: str | None
    original_start: int
    original_end: int
    output_start: int
    output_end: int
    replacement: str
    original_sha256: str
    original_text: str | None  # 仅在显式解密时填充


@dataclass(frozen=True)
class StoredRequest:
    request_id: str
    profile_name: str
    profile_version: str
    engine_version: str
    mode: str
    status: str
    error_code: str | None
    error_message: str | None
    original_length: int
    output_length: int
    redacted_output: str
    created_at: float
    finalized_at: float | None
    chunks_received: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    profile_name    TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,
    error_code      TEXT,
    error_message   TEXT,
    original_length INTEGER NOT NULL,
    output_length   INTEGER NOT NULL,
    redacted_output TEXT NOT NULL,
    created_at      REAL NOT NULL,
    finalized_at    REAL,
    chunks_received INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mappings (
    request_id     TEXT NOT NULL,
    idx            INTEGER NOT NULL,
    rule_id        TEXT NOT NULL,
    source         TEXT NOT NULL,
    key_name       TEXT,
    original_start INTEGER NOT NULL,
    original_end   INTEGER NOT NULL,
    output_start   INTEGER NOT NULL,
    output_end     INTEGER NOT NULL,
    replacement    TEXT NOT NULL,
    original_sha   TEXT NOT NULL,
    ciphertext     BLOB NOT NULL,
    PRIMARY KEY (request_id, idx),
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);
CREATE TABLE IF NOT EXISTS uncertainties (
    request_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    code       TEXT NOT NULL,
    start      INTEGER NOT NULL,
    end        INTEGER NOT NULL,
    detail     TEXT NOT NULL,
    PRIMARY KEY (request_id, idx)
);
CREATE TABLE IF NOT EXISTS events (
    request_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL
);
"""


class AuditStore:
    def __init__(self, db_path: str, fernet_key: str | bytes) -> None:
        self._path = db_path
        try:
            self._fernet = Fernet(
                fernet_key.encode() if isinstance(fernet_key, str)
                else fernet_key
            )
        except (ValueError, TypeError) as exc:
            raise AuditError(f"审计密钥非法: {exc}") from exc
        self._lock = threading.Lock()
        parent = Path(db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:
            raise AuditError(f"审计库初始化失败: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise AuditError(f"审计库写入失败: {exc}") from exc

    # ------------------------------------------------------------------ #
    def create_request(self, request_id: str, profile_name: str,
                       profile_version: str, engine_version: str,
                       mode: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO requests(request_id, profile_name, "
                "profile_version, engine_version, mode, status, "
                "original_length, output_length, redacted_output, "
                "created_at, chunks_received) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, profile_name, profile_version, engine_version,
                 mode, "open", 0, 0, "", time.time(), 0),
            )

    def record_chunk(self, request_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE requests SET chunks_received = chunks_received + 1 "
                "WHERE request_id = ?",
                (request_id,),
            )

    def finalize_request(self, request_id: str, result: RedactionResult,
                         chunks_received: int,
                         sink_events: list[dict[str, Any]]) -> None:
        """落库整个结果（映射原文加密）。事件中不含原文。"""
        with self._tx() as conn:
            conn.execute(
                "UPDATE requests SET status=?, error_code=?, error_message=?, "
                "original_length=?, output_length=?, redacted_output=?, "
                "finalized_at=?, chunks_received=? WHERE request_id=?",
                (result.status, result.error_code, result.error_message,
                 result.original_length, result.output_length,
                 result.output, time.time(), chunks_received, request_id),
            )
            for idx, m in enumerate(result.mappings):
                self._insert_mapping(conn, request_id, idx, m)
            for idx, u in enumerate(result.uncertainties):
                conn.execute(
                    "INSERT INTO uncertainties(request_id, idx, code, "
                    "start, end, detail) VALUES (?,?,?,?,?,?)",
                    (request_id, idx, u.code, u.start, u.end, u.detail),
                )
            for idx, evt in enumerate(sink_events):
                conn.execute(
                    "INSERT INTO events(request_id, idx, kind, payload) "
                    "VALUES (?,?,?,?)",
                    (request_id, idx, evt["kind"],
                     json.dumps({k: v for k, v in evt.items() if k != "kind"},
                                ensure_ascii=False)),
                )

    def _insert_mapping(self, conn: sqlite3.Connection, request_id: str,
                        idx: int, m: MappingRecord) -> None:
        # 原文在加密后写库；库文件中不出现明文片段
        if not m.original_text:
            raise AuditError(
                f"请求 {request_id} 映射 {idx} 缺少待加密原文")
        cipher = self._fernet.encrypt(m.original_text.encode("utf-8"))
        conn.execute(
            "INSERT INTO mappings(request_id, idx, rule_id, source, key_name, "
            "original_start, original_end, output_start, output_end, "
            "replacement, original_sha, ciphertext) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, idx, m.rule_id, m.source, m.key,
             m.original_start, m.original_end, m.output_start, m.output_end,
             m.replacement, m.original_sha256, cipher),
        )

    # ------------------------------------------------------------------ #
    def get_request(self, request_id: str) -> StoredRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        return StoredRequest(
            request_id=row["request_id"],
            profile_name=row["profile_name"],
            profile_version=row["profile_version"],
            engine_version=row["engine_version"],
            mode=row["mode"],
            status=row["status"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            original_length=row["original_length"],
            output_length=row["output_length"],
            redacted_output=row["redacted_output"],
            created_at=row["created_at"],
            finalized_at=row["finalized_at"],
            chunks_received=row["chunks_received"],
        )

    def list_requests(self, limit: int = 100) -> list[StoredRequest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            StoredRequest(
                request_id=r["request_id"], profile_name=r["profile_name"],
                profile_version=r["profile_version"],
                engine_version=r["engine_version"], mode=r["mode"],
                status=r["status"], error_code=r["error_code"],
                error_message=r["error_message"],
                original_length=r["original_length"],
                output_length=r["output_length"],
                redacted_output=r["redacted_output"],
                created_at=r["created_at"], finalized_at=r["finalized_at"],
                chunks_received=r["chunks_received"],
            )
            for r in rows
        ]

    def get_uncertainties(self, request_id: str) -> list[Uncertainty]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM uncertainties WHERE request_id=? ORDER BY idx",
                (request_id,),
            ).fetchall()
        return [Uncertainty(r["code"], r["start"], r["end"], r["detail"])
                for r in rows]

    def get_events(self, request_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE request_id=? ORDER BY idx",
                (request_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            payload = json.loads(r["payload"])
            out.append({"idx": r["idx"], "kind": r["kind"], **payload})
        return out

    def get_mappings(self, request_id: str, *,
                     decrypt: bool = False) -> list[StoredMapping]:
        """返回位置映射；decrypt=True 且令牌校验通过后才解密原文。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mappings WHERE request_id=? ORDER BY idx",
                (request_id,),
            ).fetchall()
        out: list[StoredMapping] = []
        for r in rows:
            original_text = None
            if decrypt:
                try:
                    original_text = self._fernet.decrypt(
                        r["ciphertext"]).decode("utf-8")
                except InvalidToken as exc:
                    raise AuditError(
                        f"映射 {r['idx']} 密文无法解密（密钥不匹配或损坏）"
                    ) from exc
            out.append(StoredMapping(
                request_id=r["request_id"], index=r["idx"],
                rule_id=r["rule_id"], source=r["source"], key=r["key_name"],
                original_start=r["original_start"],
                original_end=r["original_end"],
                output_start=r["output_start"], output_end=r["output_end"],
                replacement=r["replacement"],
                original_sha256=r["original_sha"],
                original_text=original_text,
            ))
        return out
