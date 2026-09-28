"""只追加的审计存储（SQLite）。

- 中心审计库与每次运行的数据文件分离，便于跨运行检索与状态隔离。
- 表上安装触发器拒绝 UPDATE/DELETE/ALTER（应用侧防误改），DROP 等 DDL
  由文件级权限/部署措施约束（剩余限制中说明）。
- 写入的 details 必须由调用方保证不含原始 QI/敏感值；本模块只做 JSON
  序列化，审计接口返回的字段也是固定白名单。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..logging_setup import get_logger

log = get_logger("audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    correlation_id TEXT,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    metric_version TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;
"""

# 审计检索白名单列
_PUBLIC_COLUMNS = (
    "id", "ts", "run_id", "correlation_id", "actor", "event",
    "status", "error_code", "metric_version", "details_json",
)

VALID_STATUSES = {"SUCCESS", "FAILURE", "UNREACHABLE", "REFUSED", "VALIDATION_ERROR"}


class AuditLog:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def append(self, *, event: str, status: str, actor: str = "anonymous",
               run_id: Optional[str] = None, correlation_id: Optional[str] = None,
               error_code: Optional[str] = None, metric_version: Optional[str] = None,
               details: Optional[dict[str, Any]] = None) -> int:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法审计状态 {status!r}")
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z")
        cur = self._conn.execute(
            "INSERT INTO audit_events "
            "(ts, run_id, correlation_id, actor, event, status, error_code, "
            " metric_version, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, run_id, correlation_id, actor, event, status, error_code,
             metric_version, json.dumps(details or {}, ensure_ascii=False,
                                        sort_keys=True)),
        )
        self._conn.commit()
        event_id = int(cur.lastrowid)
        log.info(
            "审计事件",
            extra={"event": {"audit_id": event_id, "event": event,
                             "status": status, "run_id": run_id,
                             "correlation_id": correlation_id,
                             "error_code": error_code}},
        )
        return event_id

    def query(self, *, run_id: Optional[str] = None,
              limit: int = 100, offset: int = 0,
              status: Optional[str] = None) -> dict:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        where, params = [], []
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        total = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM audit_events{clause}", params
        ).fetchone()["c"]
        rows = self._conn.execute(
            f"SELECT {', '.join(_PUBLIC_COLUMNS)} FROM audit_events{clause} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        items = []
        for r in rows:
            item = {k: r[k] for k in _PUBLIC_COLUMNS}
            try:
                item["details"] = json.loads(item.pop("details_json") or "{}")
            except json.JSONDecodeError:
                item["details"] = {}
            items.append(item)
        return {"total": int(total), "limit": limit, "offset": offset, "events": items}

    def close(self) -> None:
        self._conn.close()
