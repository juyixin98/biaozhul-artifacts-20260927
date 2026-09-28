"""元数据事务层（metadata catalog）。

用 SQLite 持久化表/分区/文件/列统计与裁剪审计记录。
所有"注册表"写操作都在单个 ``BEGIN IMMEDIATE`` 事务中完成，失败回滚，
保证不会留下半套元数据。schema 版本写入 ``PRAGMA user_version``。
"""
from __future__ import annotations

import json
import os
import sqlite3
import contextlib
from datetime import datetime, timezone

from .model import (Certainty, ColumnStats, FileEntry, PartitionEntry, PrunePlan,
                    TableMetadata, model_to_dict)
from .versions import (METADATA_USER_VERSION, STATS_FORMAT_VERSION,
                       DATE_TRANSFORM_VERSION)

_DDL = """
CREATE TABLE IF NOT EXISTS tables_meta(
  table_name TEXT PRIMARY KEY,
  root TEXT NOT NULL,
  partition_column TEXT NOT NULL,
  transform_json TEXT NOT NULL,
  columns_json TEXT NOT NULL,
  stats_format TEXT NOT NULL,
  date_transform TEXT NOT NULL,
  registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS partitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  part_value TEXT NOT NULL,
  UNIQUE(table_name, part_value)
);
CREATE TABLE IF NOT EXISTS files(
  file_id TEXT PRIMARY KEY,
  table_name TEXT NOT NULL,
  partition_value TEXT,
  path TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  size_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS file_stats(
  file_id TEXT NOT NULL,
  column_name TEXT NOT NULL,
  type TEXT NOT NULL,
  min_value TEXT,
  max_value TEXT,
  null_count INTEGER,
  present INTEGER NOT NULL,
  truncated INTEGER NOT NULL,
  note TEXT,
  PRIMARY KEY(file_id, column_name)
);
CREATE TABLE IF NOT EXISTS requests(
  request_id TEXT PRIMARY KEY,
  table_name TEXT NOT NULL,
  predicates_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  summary_json TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  certainty TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL,
  predicate_column TEXT,
  evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_req ON decisions(request_id);
CREATE INDEX IF NOT EXISTS idx_files_table ON files(table_name);
"""


class MetadataCatalog:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)
            cur = conn.execute("PRAGMA user_version").fetchone()
            if cur[0] != METADATA_USER_VERSION:
                conn.execute(f"PRAGMA user_version = {METADATA_USER_VERSION}")

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------ 注册
    def register_table(self, metadata: TableMetadata, root: str) -> dict:
        """整表替换注册：单事务删旧写新。"""
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute("DELETE FROM decisions WHERE request_id IN "
                         "(SELECT request_id FROM requests WHERE table_name=?)",
                         (metadata.table,))
            conn.execute("DELETE FROM requests WHERE table_name=?", (metadata.table,))
            conn.execute("DELETE FROM file_stats WHERE file_id IN "
                         "(SELECT file_id FROM files WHERE table_name=?)", (metadata.table,))
            conn.execute("DELETE FROM files WHERE table_name=?", (metadata.table,))
            conn.execute("DELETE FROM partitions WHERE table_name=?", (metadata.table,))
            conn.execute("DELETE FROM tables_meta WHERE table_name=?", (metadata.table,))

            conn.execute(
                "INSERT INTO tables_meta VALUES(?,?,?,?,?,?,?,?)",
                (metadata.table, root, metadata.partition_column,
                 json.dumps(metadata.transform), json.dumps(metadata.columns),
                 STATS_FORMAT_VERSION, DATE_TRANSFORM_VERSION, now))

            n_files = n_stats = 0
            for part in metadata.partitions:
                conn.execute("INSERT INTO partitions(table_name, part_value) VALUES(?,?)",
                             (metadata.table, part.value))
                for f in part.files:
                    n_files += 1
                    conn.execute("INSERT INTO files VALUES(?,?,?,?,?,?)",
                                 (f.file_id, metadata.table, part.value,
                                  f.physical_path, f.row_count, f.size_bytes))
                    for cname, cs in f.stats.items():
                        n_stats += 1
                        conn.execute("INSERT INTO file_stats VALUES(?,?,?,?,?,?,?,?,?)",
                                     (f.file_id, cname, cs.type,
                                      json.dumps(cs.min_value), json.dumps(cs.max_value),
                                      cs.null_count, int(cs.present), int(cs.truncated),
                                      cs.truncation_note))
        return {"table": metadata.table, "partitions": len(metadata.partitions),
                "files": n_files, "column_stats": n_stats, "registered_at": now}

    def load_table(self, table: str) -> TableMetadata:
        with self._connect() as conn:
            t = conn.execute("SELECT * FROM tables_meta WHERE table_name=?",
                             (table,)).fetchone()
            if t is None:
                raise KeyError(f"表 {table} 未注册")
            parts = {r["part_value"]: PartitionEntry(
                column=t["partition_column"], value=r["part_value"])
                for r in conn.execute("SELECT part_value FROM partitions "
                                      "WHERE table_name=? ORDER BY part_value", (table,))}
            for fr in conn.execute("SELECT * FROM files WHERE table_name=?", (table,)):
                fe = FileEntry(fr["file_id"], fr["path"], fr["partition_value"],
                               fr["row_count"], {}, fr["size_bytes"])
                for sr in conn.execute("SELECT * FROM file_stats WHERE file_id=?",
                                       (fr["file_id"],)):
                    fe.stats[sr["column_name"]] = ColumnStats(
                        column=sr["column_name"], type=sr["type"],
                        min_value=json.loads(sr["min_value"]) if sr["min_value"] is not None else None,
                        max_value=json.loads(sr["max_value"]) if sr["max_value"] is not None else None,
                        null_count=sr["null_count"], present=bool(sr["present"]),
                        truncated=bool(sr["truncated"]), truncation_note=sr["note"] or "")
                parts[fr["partition_value"]].files.append(fe)
            return TableMetadata(
                table=table, partition_column=t["partition_column"],
                transform=json.loads(t["transform_json"]),
                partitions=list(parts.values()),
                columns=json.loads(t["columns_json"]))

    def list_tables(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT table_name, partition_column, stats_format, "
                "date_transform, registered_at FROM tables_meta")]

    # ------------------------------------------------------------ 审计
    def save_plan_audit(self, plan: PrunePlan, predicates: list) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO requests VALUES(?,?,?,?,?)",
                         (plan.request_id, plan.table,
                          json.dumps(model_to_dict(predicates)), now,
                          json.dumps(plan.totals)))
            for d in plan.decisions:
                conn.execute("INSERT INTO decisions("
                             "request_id,target_type,target_id,layer,certainty,reason,"
                             "detail,predicate_column,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)",
                             (plan.request_id, d.target_type, d.target_id,
                              d.layer.value, d.certainty.value, d.reason.value,
                              d.detail, d.predicate_column, json.dumps(d.evidence)))

    def get_audit(self, request_id: str) -> dict | None:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM requests WHERE request_id=?",
                             (request_id,)).fetchone()
            if r is None:
                return None
            decs = [dict(x) for x in conn.execute(
                "SELECT * FROM decisions WHERE request_id=?", (request_id,))]
            return {"request_id": r["request_id"], "table": r["table_name"],
                    "created_at": r["created_at"],
                    "predicates": json.loads(r["predicates_json"]),
                    "summary": json.loads(r["summary_json"]),
                    "decisions": decs}
