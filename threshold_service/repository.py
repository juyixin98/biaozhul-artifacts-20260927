"""状态隔离层：SQLite 仓储。

职责：
- 集合（share_set）与份额（share）持久化，按 set_id 严格隔离；
- 份额信封在盘上经 Fernet（AES-128-CBC + HMAC）加密保管，
  主密钥经 KDF 独立派生存储密钥（与份额标签密钥不同域）；
- 只落必要元数据与**份额指纹**（日志/去重用），不冗余明文 y；
- 连接级 WAL + 外键约束；每个测试库独立文件，便于隔离。

明确取舍：本地演示采用"服务端代管份额"模式以便端到端演练。
真正的 Shamir 部署中份额应由各参与者自行保管、永不集中存储，
README 的"支持范围与取舍"一节对此有说明。
"""
from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_STORAGE_SALT = b"tss-at-rest-storage/v1"


def _storage_fernet(master_key: bytes) -> Fernet:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_STORAGE_SALT, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(master_key)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, database_path: str, master_key: bytes):
        self.database_path = database_path
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._fernet = _storage_fernet(master_key)
        self._init_schema()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.database_path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS share_set (
                    set_id       TEXT PRIMARY KEY,
                    threshold    INTEGER NOT NULL,
                    field_bits   INTEGER NOT NULL,
                    field_gen    INTEGER NOT NULL,
                    secret_len   INTEGER NOT NULL,
                    commitment   TEXT NOT NULL,
                    secret_fp    TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    labels       TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS share (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    set_id       TEXT NOT NULL REFERENCES share_set(set_id),
                    x            INTEGER NOT NULL,
                    fingerprint  TEXT NOT NULL,
                    label        TEXT,
                    envelope_enc BLOB NOT NULL,
                    issued_at    TEXT NOT NULL,
                    UNIQUE(set_id, x),
                    UNIQUE(set_id, fingerprint)
                );
                """
            )

    # ---- 集合 ----
    def save_set(
        self,
        *,
        set_id: str,
        threshold: int,
        field_bits: int,
        field_gen: int,
        secret_len: int,
        commitment: str,
        secret_fp: str,
        labels: dict[str, str],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO share_set
                   (set_id, threshold, field_bits, field_gen, secret_len,
                    commitment, secret_fp, created_at, labels)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (set_id, threshold, field_bits, field_gen, secret_len,
                 commitment, secret_fp, _now(), json.dumps(labels, sort_keys=True)),
            )

    def get_set(self, set_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            cur = conn.execute("SELECT * FROM share_set WHERE set_id = ?", (set_id,))
            return cur.fetchone()

    def list_sets(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT set_id, threshold, field_bits, field_gen,"
                                     " secret_len, secret_fp, created_at FROM share_set"
                                     " ORDER BY created_at"))

    # ---- 份额 ----
    def save_share(self, *, set_id: str, x: int, fingerprint: str,
                   label: str | None, envelope: dict) -> None:
        token = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO share (set_id, x, fingerprint, label, envelope_enc, issued_at)
                   VALUES (?,?,?,?,?,?)""",
                (set_id, x, fingerprint, label, self._fernet.encrypt(token), _now()),
            )

    def get_shares(self, set_id: str) -> list[dict]:
        """仅管理面使用：返回解密后的份额（按 x 排序）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT envelope_enc FROM share WHERE set_id = ? ORDER BY x", (set_id,)
            ).fetchall()
        return [json.loads(self._fernet.decrypt(r["envelope_enc"])) for r in rows]

    def share_fingerprints(self, set_id: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT fingerprint FROM share WHERE set_id = ?", (set_id,)
            ).fetchall()
        return {r["fingerprint"] for r in rows}
