"""SQLite 版本存储。

表设计
======
``sources``       每个源一行，保存当前版本与当前文本（显式版本化见 versions 表）
``source_versions`` 不可变版本：每次写入/应用产出一行（version, sha256, 长度, 时间）
                  文本正文以 gzip blob 存 ``version_bodies``，避免大文本重复膨胀
``rules``         计划内规则的归一化 JSON
``plans``         计划头：绑定 source_id + source_version + 源摘要
``plan_entries``  有序替换条目（码点/字节范围、匹配与替换文本、捕获快照 JSON）
``plan_displaced`` 因重叠消解被淘汰的命中（诊断）
``diag_events``   带运行编号的结构化诊断事件

线程策略：每次打开连接用 ``check_same_thread=False`` + 进程内一把锁串行写；
FastAPI 依赖用单一仓库实例。全部为本地文件，无外部参与者。
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .textutil import TextSpec, make_spec

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  source_id     TEXT PRIMARY KEY,
  current_version INTEGER NOT NULL,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS source_versions(
  source_id TEXT NOT NULL,
  version   INTEGER NOT NULL,
  sha256    TEXT NOT NULL,
  byte_len  INTEGER NOT NULL,
  char_len  INTEGER NOT NULL,
  origin    TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(source_id, version)
);
CREATE TABLE IF NOT EXISTS version_bodies(
  source_id TEXT NOT NULL,
  version   INTEGER NOT NULL,
  gz_text   BLOB NOT NULL,
  PRIMARY KEY(source_id, version)
);
CREATE TABLE IF NOT EXISTS plans(
  plan_id        TEXT PRIMARY KEY,
  source_id      TEXT NOT NULL,
  source_version INTEGER NOT NULL,
  sha256         TEXT NOT NULL,
  byte_len       INTEGER NOT NULL,
  char_len       INTEGER NOT NULL,
  status         TEXT NOT NULL CHECK(status IN ('planned','applied')),
  applied_version INTEGER,
  rule_count     INTEGER NOT NULL,
  created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules(
  plan_id TEXT NOT NULL,
  declaration_order INTEGER NOT NULL,
  rule_json TEXT NOT NULL,
  PRIMARY KEY(plan_id, declaration_order)
);
CREATE TABLE IF NOT EXISTS plan_entries(
  plan_id TEXT NOT NULL,
  idx     INTEGER NOT NULL,
  rule_id TEXT NOT NULL,
  priority INTEGER NOT NULL,
  declaration_order INTEGER NOT NULL,
  char_start INTEGER NOT NULL,
  char_end   INTEGER NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end   INTEGER NOT NULL,
  matched TEXT NOT NULL,
  replacement TEXT NOT NULL,
  zero_width INTEGER NOT NULL,
  groups_json TEXT NOT NULL,
  PRIMARY KEY(plan_id, idx)
);
CREATE TABLE IF NOT EXISTS plan_displaced(
  plan_id TEXT NOT NULL,
  seq     INTEGER NOT NULL,
  rule_id TEXT NOT NULL,
  char_start INTEGER NOT NULL,
  char_end   INTEGER NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY(plan_id, seq)
);
CREATE TABLE IF NOT EXISTS diag_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  level TEXT NOT NULL,
  event TEXT NOT NULL,
  message TEXT NOT NULL,
  data_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_diag_run ON diag_events(run_id, id);
CREATE INDEX IF NOT EXISTS ix_plans_source ON plans(source_id);
"""


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), compresslevel=6)


def _ungz(blob: bytes) -> str:
    return gzip.decompress(blob).decode("utf-8")


class Repository:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ------------------------------------------------------------------ #
    # 诊断事件
    # ------------------------------------------------------------------ #
    def add_diag(
        self,
        run_id: str,
        stage: str,
        level: str,
        event: str,
        message: str,
        data: dict | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO diag_events(run_id,stage,level,event,message,data_json,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (run_id, stage, level, event, message, json.dumps(data or {}, ensure_ascii=False),
                 time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_diag(self, run_id: str, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,run_id,stage,level,event,message,data_json FROM diag_events"
                " WHERE run_id=? ORDER BY id LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [
            {
                "seq": r["id"],
                "run_id": r["run_id"],
                "stage": r["stage"],
                "level": r["level"],
                "event": r["event"],
                "message": r["message"],
                "data": json.loads(r["data_json"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # 源与版本
    # ------------------------------------------------------------------ #
    def create_source(self, text: str) -> dict:
        now = time.time()
        source_id = "src-" + uuid.uuid4().hex[:16]
        spec = make_spec(text)
        with self._lock:
            self._conn.execute(
                "INSERT INTO sources(source_id,current_version,created_at,updated_at)"
                " VALUES(?,?,?,?)",
                (source_id, 1, now, now),
            )
            self._add_version(source_id, 1, spec, text, "upload", now)
            self._conn.commit()
        return {
            "source_id": source_id,
            "version": 1,
            "spec": spec.__dict__,
        }

    def _add_version(
        self, source_id: str, version: int, spec: TextSpec, text: str, origin: str, now: float
    ) -> None:
        self._conn.execute(
            "INSERT INTO source_versions(source_id,version,sha256,byte_len,char_len,origin,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (source_id, version, spec.sha256, spec.byte_len, spec.char_len, origin, now),
        )
        self._conn.execute(
            "INSERT INTO version_bodies(source_id,version,gz_text) VALUES(?,?,?)",
            (source_id, version, _gz(text)),
        )

    def get_source(self, source_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT s.source_id,s.current_version,v.sha256,v.byte_len,v.char_len"
                " FROM sources s JOIN source_versions v"
                " ON v.source_id=s.source_id AND v.version=s.current_version"
                " WHERE s.source_id=?",
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_id": row["source_id"],
            "version": row["current_version"],
            "spec": {
                "sha256": row["sha256"],
                "byte_len": row["byte_len"],
                "char_len": row["char_len"],
            },
        }

    def get_text(self, source_id: str, version: int | None = None) -> tuple[str, int, TextSpec] | None:
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    "SELECT current_version FROM sources WHERE source_id=?", (source_id,)
                ).fetchone()
                if row is None:
                    return None
                version = row["current_version"]
            body = self._conn.execute(
                "SELECT b.gz_text,v.sha256,v.byte_len,v.char_len FROM version_bodies b"
                " JOIN source_versions v ON v.source_id=b.source_id AND v.version=b.version"
                " WHERE b.source_id=? AND b.version=?",
                (source_id, version),
            ).fetchone()
        if body is None:
            return None
        spec = TextSpec(body["sha256"], body["byte_len"], body["char_len"])
        return _ungz(body["gz_text"]), version, spec

    def update_source_text(self, source_id: str, text: str) -> dict:
        """写入新版本（源内容变更）。返回新版本描述；供版本不符测试使用。"""
        current = self.get_source(source_id)
        if current is None:
            from .errors import SourceNotFoundError

            raise SourceNotFoundError("unknown source_id", details={"source_id": source_id})
        new_version = current["version"] + 1
        spec = make_spec(text)
        now = time.time()
        with self._lock:
            self._add_version(source_id, new_version, spec, text, "upload", now)
            self._conn.execute(
                "UPDATE sources SET current_version=?, updated_at=? WHERE source_id=?",
                (new_version, now, source_id),
            )
            self._conn.commit()
        return {"source_id": source_id, "version": new_version, "spec": spec.__dict__}

    # ------------------------------------------------------------------ #
    # 计划
    # ------------------------------------------------------------------ #
    def save_plan(self, plan: Plan, source_id: str, source_version: int, rules_in: list[RuleIn]) -> str:
        plan_id = "plan-" + uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO plans(plan_id,source_id,source_version,sha256,byte_len,char_len,"
                "status,applied_version,rule_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, source_id, source_version, plan.source_spec.sha256,
                    plan.source_spec.byte_len, plan.source_spec.char_len,
                    "planned", None, len(plan.rules), now,
                ),
            )
            for d, ri in enumerate(rules_in):
                self._conn.execute(
                    "INSERT INTO rules(plan_id,declaration_order,rule_json) VALUES(?,?,?)",
                    (plan_id, d, ri.model_dump_json()),
                )
            for idx, cand in enumerate(plan.chosen):
                groups = [
                    {
                        "index": g.index,
                        "name": g.name,
                        "text": g.text,
                        "char_start": g.char_start,
                        "char_end": g.char_end,
                    }
                    for g in cand.hit.groups
                ]
                self._conn.execute(
                    "INSERT INTO plan_entries(plan_id,idx,rule_id,priority,declaration_order,"
                    "char_start,char_end,byte_start,byte_end,matched,replacement,zero_width,groups_json)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id, idx, cand.rule.rule_id, cand.rule.priority,
                        cand.rule.declaration_order, cand.hit.start, cand.hit.end,
                        cand.byte_start, cand.byte_end, cand.hit.text, cand.replacement,
                        1 if cand.hit.is_zero_width else 0, json.dumps(groups, ensure_ascii=False),
                    ),
                )
            for seq, d in enumerate(plan.displaced):
                self._conn.execute(
                    "INSERT INTO plan_displaced(plan_id,seq,rule_id,char_start,char_end,reason)"
                    " VALUES(?,?,?,?,?,?)",
                    (plan_id, seq, d.rule_id, d.char_start, d.char_end, d.reason),
                )
            self._conn.commit()
        return plan_id

    def get_plan_header(self, plan_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()

    def list_plans(self, source_id: str | None = None) -> list[dict]:
        with self._lock:
            if source_id is None:
                rows = self._conn.execute("SELECT * FROM plans ORDER BY created_at").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM plans WHERE source_id=? ORDER BY created_at", (source_id,)
                ).fetchall()
        return [dict(r) for r in rows]

    def mark_plan_applied(self, plan_id: str, output_version: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE plans SET status='applied', applied_version=? WHERE plan_id=?",
                (output_version, plan_id),
            )
            self._conn.commit()

    def add_applied_version(self, source_id: str, output_text: str) -> int:
        """把应用结果登记为源的新版本，返回版本号。"""
        current = self.get_source(source_id)
        if current is None:
            from .errors import SourceNotFoundError

            raise SourceNotFoundError("unknown source_id", details={"source_id": source_id})
        new_version = current["version"] + 1
        spec = make_spec(output_text)
        now = time.time()
        with self._lock:
            self._add_version(source_id, new_version, spec, output_text, "plan_apply", now)
            self._conn.execute(
                "UPDATE sources SET current_version=?, updated_at=? WHERE source_id=?",
                (new_version, now, source_id),
            )
            self._conn.commit()
        return new_version

    def get_plan_entries(self, plan_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM plan_entries WHERE plan_id=? ORDER BY idx", (plan_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_plan_rules(self, plan_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT rule_json FROM rules WHERE plan_id=? ORDER BY declaration_order",
                (plan_id,),
            ).fetchall()
        return [json.loads(r["rule_json"]) for r in rows]

    def get_plan_displaced(self, plan_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT rule_id,char_start,char_end,reason FROM plan_displaced"
                " WHERE plan_id=? ORDER BY seq",
                (plan_id,),
            ).fetchall()
        return [dict(r) for r in rows]
