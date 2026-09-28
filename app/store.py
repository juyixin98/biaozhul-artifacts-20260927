r"""作业状态：SQLite 持久化 + 原始字节落盘。

状态机::

    PENDING -> RUNNING -> SUCCEEDED
                       \-> FAILED   （终态，不可重试同一作业；重复操作返回
                                      STATE_CONFLICT）

状态转换用条件 UPDATE 做乐观 CAS，保证“重复提交/重复执行”不会双跑。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SegmentError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    sample_rate INTEGER,
    total_samples INTEGER,
    params_json TEXT,
    config_json TEXT,
    intervals_json TEXT,
    raw_ranges_json TEXT,
    error_code TEXT,
    error_category TEXT,
    error_message TEXT,
    error_details_json TEXT,
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem
    ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
"""


@dataclass
class JobRecord:
    id: str
    run_id: str
    status: str
    created_at: float
    updated_at: float
    sample_rate: int | None = None
    total_samples: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    intervals: list[list[int]] = field(default_factory=list)
    raw_ranges: list[list[int]] = field(default_factory=list)
    error_code: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "job_id": self.id,
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.status == "SUCCEEDED":
            out.update(
                sample_rate=self.sample_rate,
                total_samples=self.total_samples,
                intervals=self.intervals,
                raw_ranges=self.raw_ranges,
                params=self.params,
            )
        if self.status == "FAILED":
            out.update(
                error={
                    "code": self.error_code,
                    "category": self.error_category,
                    "message": self.error_message,
                    "details": self.error_details,
                }
            )
        return out


class JobStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audio_dir = self.data_dir / "audio"
        self.audio_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "jobs.db"
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 原始字节 ----

    def save_audio(self, job_id: str, data: bytes) -> Path:
        path = self.audio_dir / f"{job_id}.bin"
        path.write_bytes(data)
        return path

    def load_audio(self, job_id: str) -> bytes:
        path = self.audio_dir / f"{job_id}.bin"
        if not path.exists():
            raise SegmentError(
                "JOB_NOT_FOUND", "audio bytes for job are missing on disk",
                job_id=job_id,
            )
        return path.read_bytes()

    # ---- CRUD ----

    def create(self, rec: JobRecord) -> None:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO jobs(id, run_id, status, created_at, updated_at,
                          config_json, idempotency_key)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        rec.id, rec.run_id, rec.status, rec.created_at,
                        rec.updated_at, json.dumps(rec.config),
                        rec.idempotency_key,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # 唯一索引冲突 => 幂等键重复（调用方据此决定 STATE_CONFLICT）。
                raise SegmentError(
                    "STATE_CONFLICT",
                    "idempotency_key already used",
                    idempotency_key=rec.idempotency_key,
                ) from exc

    def get(self, job_id: str) -> JobRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise SegmentError("JOB_NOT_FOUND", "job not found", job_id=job_id)
        return _row_to_record(row)

    def find_by_idempotency_key(self, key: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def cas_status(
        self, expected: str, new: str, job_id: str, **fields: Any
    ) -> bool:
        """条件更新：仅当当前状态为 ``expected`` 时改为 ``new``。"""
        cols = ["status = ?", "updated_at = ?"]
        vals: list[Any] = [new, fields.pop("updated_at")]
        mapping = {
            "sample_rate": "sample_rate",
            "total_samples": "total_samples",
            "params": "params_json",
            "intervals": "intervals_json",
            "raw_ranges": "raw_ranges_json",
            "error_code": "error_code",
            "error_category": "error_category",
            "error_message": "error_message",
            "error_details": "error_details_json",
        }
        for key, val in fields.items():
            if key not in mapping:
                raise SegmentError(
                    "INTERNAL", "unknown job field in update", field=key
                )
            col = mapping[key]
            cols.append(f"{col} = ?")
            if isinstance(val, (dict, list)):
                vals.append(json.dumps(val))
            else:
                vals.append(val)
        vals.extend([job_id, expected])
        sql = f"UPDATE jobs SET {', '.join(cols)} WHERE id = ? AND status = ?"
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, vals)
            return cur.rowcount == 1


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    def loads(name: str, default: Any) -> Any:
        v = row[name]
        return json.loads(v) if v is not None else default

    return JobRecord(
        id=row["id"],
        run_id=row["run_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        sample_rate=row["sample_rate"],
        total_samples=row["total_samples"],
        params=loads("params_json", {}),
        config=loads("config_json", {}),
        intervals=loads("intervals_json", []),
        raw_ranges=loads("raw_ranges_json", []),
        error_code=row["error_code"],
        error_category=row["error_category"],
        error_message=row["error_message"],
        error_details=loads("error_details_json", {}),
        idempotency_key=row["idempotency_key"],
    )
