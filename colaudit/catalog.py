"""SQLite 元数据事务: 数据集登记、审计运行、列裁决与诊断事件。

每次审计在单个事务内写入: audit_runs + column_verdicts + diagnostic_events,
任一写入失败整体回滚, 保证不会留下"半份审计报告"。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name        TEXT PRIMARY KEY,
    root        TEXT NOT NULL,
    columns     TEXT NOT NULL,           -- JSON
    sensitive   TEXT NOT NULL,           -- JSON list
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_runs (
    run_id      TEXT PRIMARY KEY,
    dataset     TEXT NOT NULL REFERENCES datasets(name),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,           -- running | ok | error
    summary     TEXT,                    -- JSON
    error       TEXT
);

CREATE TABLE IF NOT EXISTS column_verdicts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES audit_runs(run_id),
    dataset     TEXT NOT NULL,
    file        TEXT NOT NULL,
    row_group   INTEGER NOT NULL,
    scope       TEXT NOT NULL,           -- page | row_group
    page        INTEGER,                 -- scope=page 时有效
    column_name TEXT NOT NULL,
    verdict     TEXT NOT NULL,           -- 见 audit.Verdict
    trusted     INTEGER NOT NULL,        -- 0/1: 是否允许用于剪枝
    failure     TEXT,                    -- 失败类别 (verdict!=ok 时)
    detail      TEXT,                    -- JSON
    UNIQUE(run_id, file, row_group, scope, page, column_name)
);

CREATE TABLE IF NOT EXISTS diagnostic_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES audit_runs(run_id),
    request_id  TEXT,
    occurred_at TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    file        TEXT,
    row_group   INTEGER,
    scope       TEXT,
    page        INTEGER,
    column_name TEXT,
    code        TEXT NOT NULL,
    decision    TEXT NOT NULL,           -- accept | reject | unknown
    message     TEXT NOT NULL,
    state       TEXT NOT NULL            -- JSON, 敏感值已脱敏
);

CREATE INDEX IF NOT EXISTS idx_verdicts_run ON column_verdicts(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON diagnostic_events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_request ON diagnostic_events(request_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- 数据集 -----------------------------------------------------------
    def register_dataset(
        self,
        name: str,
        root: str | Path,
        columns: list[dict[str, Any]],
        sensitive: Iterable[str],
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO datasets(name, root, columns, sensitive, registered_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     root=excluded.root,
                     columns=excluded.columns,
                     sensitive=excluded.sensitive,
                     registered_at=excluded.registered_at""",
                (
                    name,
                    str(root),
                    json.dumps(columns, ensure_ascii=False),
                    json.dumps(list(sensitive), ensure_ascii=False),
                    _utcnow(),
                ),
            )

    def get_dataset(self, name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM datasets WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["columns"] = json.loads(d["columns"])
        d["sensitive"] = json.loads(d["sensitive"])
        return d

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, root, sensitive, registered_at FROM datasets "
                "ORDER BY name"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sensitive"] = json.loads(d["sensitive"])
            out.append(d)
        return out

    # ---- 审计运行 ---------------------------------------------------------
    def start_run(self, run_id: str, dataset: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_runs(run_id, dataset, started_at, status) "
                "VALUES (?, ?, ?, 'running')",
                (run_id, dataset, _utcnow()),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE audit_runs SET finished_at=?, status=?, summary=?, "
                "error=? WHERE run_id=?",
                (
                    _utcnow(),
                    status,
                    json.dumps(summary, ensure_ascii=False) if summary else None,
                    error,
                    run_id,
                ),
            )

    def save_report(self, report: dict[str, Any]) -> None:
        """在一个事务内落库完整报告 (运行、列裁决、诊断事件)。"""
        run = report["run_id"]
        dataset = report["dataset"]
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM datasets WHERE name = ?", (dataset,)
            ).fetchone()
            if exists is None:
                raise sqlite3.IntegrityError(
                    f"数据集未登记, 拒绝写入审计: {dataset}"
                )
            conn.execute(
                "INSERT INTO audit_runs(run_id, dataset, started_at, "
                "finished_at, status, summary) VALUES (?, ?, ?, ?, 'ok', ?)",
                (
                    run,
                    dataset,
                    report.get("started_at", _utcnow()),
                    report.get("finished_at", _utcnow()),
                    json.dumps(report.get("summary", {}), ensure_ascii=False),
                ),
            )
            for v in report["verdicts"]:
                conn.execute(
                    """INSERT INTO column_verdicts(run_id, dataset, file,
                         row_group, scope, page, column_name, verdict,
                         trusted, failure, detail)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run,
                        dataset,
                        v["file"],
                        v["row_group"],
                        v["scope"],
                        v.get("page"),
                        v["column_name"],
                        v["verdict"],
                        1 if v["trusted"] else 0,
                        v.get("failure"),
                        json.dumps(v.get("detail", {}), ensure_ascii=False),
                    ),
                )
            for e in report["diagnostics"]:
                conn.execute(
                    """INSERT INTO diagnostic_events(run_id, request_id,
                         occurred_at, dataset, file, row_group, scope, page,
                         column_name, code, decision, message, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run,
                        e.get("request_id"),
                        e.get("occurred_at", _utcnow()),
                        dataset,
                        e.get("file"),
                        e.get("row_group"),
                        e.get("scope"),
                        e.get("page"),
                        e.get("column_name"),
                        e["code"],
                        e["decision"],
                        e["message"],
                        json.dumps(e.get("state", {}), ensure_ascii=False),
                    ),
                )

    def load_report(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            run = conn.execute(
                "SELECT * FROM audit_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            verdicts = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM column_verdicts WHERE run_id=? "
                    "ORDER BY file, row_group, scope, page, column_name",
                    (run_id,),
                ).fetchall()
            ]
            events = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM diagnostic_events WHERE run_id=? "
                    "ORDER BY id",
                    (run_id,),
                ).fetchall()
            ]
        for v in verdicts:
            v["trusted"] = bool(v["trusted"])
            v["detail"] = json.loads(v["detail"]) if v["detail"] else {}
        for e in events:
            e["state"] = json.loads(e["state"]) if e["state"] else {}
        run = dict(run)
        run["summary"] = json.loads(run["summary"]) if run["summary"] else {}
        return {"run": run, "verdicts": verdicts, "diagnostics": events}

    def latest_run_id(self, dataset: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM audit_runs WHERE dataset=? AND status='ok' "
                "ORDER BY finished_at DESC LIMIT 1",
                (dataset,),
            ).fetchone()
        return row["run_id"] if row else None

    def trusted_page_verdicts(self, run_id: str) -> dict[tuple, dict[str, Any]]:
        """{(file, rg, page): {column: verdict_row}} 仅受信页级裁决。"""
        out: dict[tuple, dict[str, Any]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM column_verdicts WHERE run_id=? AND scope='page'",
                (run_id,),
            ).fetchall()
        for r in rows:
            key = (r["file"], r["row_group"], r["page"])
            d = out.setdefault(key, {})
            row = dict(r)
            row["trusted"] = bool(row["trusted"])
            d[r["column_name"]] = row
        return out
