"""元数据事务层：SQLite 存分支/提交/血缘，Parquet 文件存不可变快照。

关键不变量：
* snapshot / commit / branch 更新在同一个 BEGIN IMMEDIATE 事务内完成，
  任何一步失败整体回滚，绝不留下“有快照没提交”或“分支头悬空”的状态；
* 快照按内容哈希幂等去重，相同数据重复导入返回同一 snapshot_id，
  但分支推进只能通过新提交完成，不能通过重新导入覆盖历史；
* merge_commits 同时保存两条父引用（parent1_dev / parent2_main）与共同祖先。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import ConflictStateError, NotFoundError
from .format_adapter import read_parquet, write_parquet
from .models import Snapshot, TableSchema

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    table_name    TEXT NOT NULL,
    schema_json   TEXT NOT NULL,
    parquet_path  TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS commits (
    commit_id        TEXT PRIMARY KEY,
    snapshot_id      TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    parent_commit_id TEXT REFERENCES commits(commit_id),
    message          TEXT NOT NULL,
    author           TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS merge_commits (
    commit_id          TEXT PRIMARY KEY REFERENCES commits(commit_id),
    base_commit_id     TEXT NOT NULL REFERENCES commits(commit_id),
    parent1_dev_id     TEXT NOT NULL REFERENCES commits(commit_id),
    parent2_main_id    TEXT NOT NULL REFERENCES commits(commit_id),
    base_snapshot_id   TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    parent1_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    parent2_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    resolution_summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_resolutions (
    plan_id          TEXT NOT NULL,
    row_key          TEXT NOT NULL,
    decision         TEXT NOT NULL,
    action           TEXT NOT NULL,
    field_picks_json TEXT,
    base_snapshot_id   TEXT NOT NULL,
    dev_snapshot_id    TEXT NOT NULL,
    main_snapshot_id   TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (plan_id, row_key)
);
CREATE TABLE IF NOT EXISTS branches (
    name       TEXT PRIMARY KEY,
    commit_id  TEXT NOT NULL REFERENCES commits(commit_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_commits_parent ON commits(parent_commit_id);
CREATE INDEX IF NOT EXISTS idx_commits_snapshot ON commits(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_resolutions_plan ON merge_resolutions(plan_id);
"""


class MetadataStore:
    def __init__(self, db_path: str | Path, snapshot_dir: str | Path):
        self.db_path = Path(db_path)
        self.snapshot_dir = Path(snapshot_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    # ---- 连接 / 事务 -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def transaction(self):
        """单写事务：BEGIN IMMEDIATE 立即拿写锁，失败回滚。"""
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

    # ---- 快照：内容寻址、幂等 ---------------------------------------------

    def materialize_snapshot(
        self,
        rows: list[dict[str, Any]],
        schema: TableSchema,
    ) -> Snapshot:
        """校验并写出快照；相同内容返回既有快照（幂等导入，不覆盖任何历史）。

        去重以写盘后的 Parquet 字节 sha256 为准：内容寻址保证不可变快照
        不会因重复导入产生两份历史。文件写盘独立于提交事务（写盘幂等、
        可安全重试），事务只负责元数据与分支指针的原子更新。
        """
        snapshot_id = "snap_" + uuid.uuid4().hex[:16]
        rel_path = f"{schema.table}/{snapshot_id}.parquet"
        content_hash = write_parquet(rows, schema, self.snapshot_dir / rel_path)

        conn = self._connect()
        try:
            dup = conn.execute(
                "SELECT * FROM snapshots WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if dup is not None:
                # 刚写出的文件是冗余副本，删掉并复用既有快照
                try:
                    (self.snapshot_dir / rel_path).unlink()
                except OSError:
                    pass
                cached = self._row_to_snapshot(dup)
                return Snapshot(
                    snapshot_id=cached.snapshot_id, table=cached.table, schema=cached.schema,
                    parquet_path=cached.parquet_path, row_count=cached.row_count,
                    content_hash=cached.content_hash, reused=True,
                )

            conn.execute(
                "INSERT INTO snapshots "
                "(snapshot_id, table_name, schema_json, parquet_path, row_count, content_hash) "
                "VALUES (:snapshot_id, :table_name, :schema_json, :parquet_path, "
                ":row_count, :content_hash) "
                "ON CONFLICT(content_hash) DO NOTHING",
                {
                    "snapshot_id": snapshot_id,
                    "table_name": schema.table,
                    "schema_json": json.dumps(schema.to_dict(), sort_keys=True),
                    "parquet_path": rel_path,
                    "row_count": len(rows),
                    "content_hash": content_hash,
                },
            )
            winner = conn.execute(
                "SELECT * FROM snapshots WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            conn.commit()
            if winner["snapshot_id"] != snapshot_id:
                # 并发下另一进程先注册了相同内容；复用它并清理自己的副本
                try:
                    (self.snapshot_dir / rel_path).unlink()
                except OSError:
                    pass
                cached = self._row_to_snapshot(winner)
                return Snapshot(
                    snapshot_id=cached.snapshot_id, table=cached.table, schema=cached.schema,
                    parquet_path=cached.parquet_path, row_count=cached.row_count,
                    content_hash=cached.content_hash, reused=True,
                )
            return Snapshot(
                snapshot_id=snapshot_id, table=schema.table, schema=schema,
                parquet_path=rel_path, row_count=len(rows), content_hash=content_hash,
            )
        except Exception:
            # 元数据插入失败时不留下孤儿文件
            try:
                (self.snapshot_dir / rel_path).unlink()
            except OSError:
                pass
            raise
        finally:
            conn.close()

    def _row_to_snapshot(self, row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            snapshot_id=row["snapshot_id"],
            table=row["table_name"],
            schema=TableSchema.from_dict(json.loads(row["schema_json"])),
            parquet_path=row["parquet_path"],
            row_count=row["row_count"],
            content_hash=row["content_hash"],
        )

    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"snapshot {snapshot_id!r} not found",
                                details={"snapshot_id": snapshot_id})
        return self._row_to_snapshot(row)

    def read_snapshot_rows(self, snapshot_id: str) -> tuple[Snapshot, list[dict]]:
        snap = self.get_snapshot(snapshot_id)
        rows = read_parquet(self.snapshot_dir / snap.parquet_path)
        return snap, rows

    # ---- 提交（线性）------------------------------------------------------

    def create_commit(
        self,
        snapshot_id: str,
        parent_commit_id: str | None,
        message: str,
        author: str,
        branch_name: str | None = None,
    ) -> dict:
        """在单事务内创建提交并（可选）推进分支头。

        branch_name 非空时，分支必须已存在且当前头 == parent_commit_id，
        防止把提交挂到别人已经推进过的分支上（丢失更新）。
        """
        with self.transaction() as conn:
            self._require_snapshot_row(conn, snapshot_id)
            if parent_commit_id is not None:
                self._require_commit_row(conn, parent_commit_id)
            commit_id = "commit_" + uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO commits (commit_id, snapshot_id, parent_commit_id, message, author) "
                "VALUES (?, ?, ?, ?, ?)",
                (commit_id, snapshot_id, parent_commit_id, message, author),
            )
            if branch_name is not None:
                self._advance_branch(conn, branch_name, commit_id, parent_commit_id)
        return self.get_commit(commit_id)

    def create_branch(self, name: str, commit_id: str) -> dict:
        with self.transaction() as conn:
            self._require_commit_row(conn, commit_id)
            exists = conn.execute(
                "SELECT 1 FROM branches WHERE name = ?", (name,)
            ).fetchone()
            if exists is not None:
                raise ConflictStateError(f"branch {name!r} already exists",
                                         details={"branch": name})
            conn.execute(
                "INSERT INTO branches (name, commit_id) VALUES (?, ?)",
                (name, commit_id),
            )
        return self.get_branch(name)

    def _advance_branch(self, conn: sqlite3.Connection, branch_name: str,
                        new_commit_id: str, expected_parent: str | None) -> None:
        row = conn.execute(
            "SELECT commit_id FROM branches WHERE name = ?", (branch_name,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"branch {branch_name!r} not found",
                                details={"branch": branch_name})
        if row["commit_id"] != expected_parent:
            raise ConflictStateError(
                f"branch {branch_name!r} moved since parent was read "
                f"(head={row['commit_id']}, expected={expected_parent}); refusing to overwrite",
                details={"branch": branch_name, "current_head": row["commit_id"],
                         "expected_parent": expected_parent},
            )
        conn.execute(
            "UPDATE branches SET commit_id = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE name = ?",
            (new_commit_id, branch_name),
        )

    # ---- 合并提交（两条父引用）--------------------------------------------

    def create_merge_commit(
        self,
        *,
        snapshot_id: str,
        base_commit_id: str,
        parent1_dev_commit_id: str,
        parent2_main_commit_id: str,
        base_snapshot_id: str,
        parent1_snapshot_id: str,
        parent2_snapshot_id: str,
        resolution_summary: dict,
        target_branch: str,
        expected_head_commit_id: str,
        message: str,
        author: str,
    ) -> dict:
        """合并提交：commits 行的 parent 记主分支父提交；merge_commits 保留双亲与祖先。"""
        with self.transaction() as conn:
            for sid in (snapshot_id, base_snapshot_id, parent1_snapshot_id, parent2_snapshot_id):
                self._require_snapshot_row(conn, sid)
            for cid in (base_commit_id, parent1_dev_commit_id, parent2_main_commit_id):
                self._require_commit_row(conn, cid)
            commit_id = "merge_" + uuid.uuid4().hex[:16]
            # 主分支线性视角的父提交是 main 侧父提交；dev 侧父提交保存在 merge_commits
            conn.execute(
                "INSERT INTO commits (commit_id, snapshot_id, parent_commit_id, message, author) "
                "VALUES (?, ?, ?, ?, ?)",
                (commit_id, snapshot_id, parent2_main_commit_id, message, author),
            )
            conn.execute(
                "INSERT INTO merge_commits (commit_id, base_commit_id, parent1_dev_id, "
                "parent2_main_id, base_snapshot_id, parent1_snapshot_id, parent2_snapshot_id, "
                "resolution_summary_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (commit_id, base_commit_id, parent1_dev_commit_id, parent2_main_commit_id,
                 base_snapshot_id, parent1_snapshot_id, parent2_snapshot_id,
                 json.dumps(resolution_summary, sort_keys=True)),
            )
            self._advance_branch(conn, target_branch, commit_id, expected_head_commit_id)
        return self.get_commit(commit_id)

    # ---- 冲突解决（绑定三方快照）------------------------------------------

    def save_resolutions(self, plan_id: str, resolutions: list[dict]) -> int:
        """幂等保存一份计划的解决结果；行键已存在则更新，动作仍带三方快照 ID。"""
        saved = 0
        with self.transaction() as conn:
            for item in resolutions:
                conn.execute(
                    "INSERT INTO merge_resolutions "
                    "(plan_id, row_key, decision, action, field_picks_json, "
                    " base_snapshot_id, dev_snapshot_id, main_snapshot_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(plan_id, row_key) DO UPDATE SET "
                    "decision=excluded.decision, action=excluded.action, "
                    "field_picks_json=excluded.field_picks_json, "
                    "base_snapshot_id=excluded.base_snapshot_id, "
                    "dev_snapshot_id=excluded.dev_snapshot_id, "
                    "main_snapshot_id=excluded.main_snapshot_id",
                    (
                        plan_id,
                        item["row_key"],
                        item["decision"],
                        item["action"],
                        json.dumps(item.get("field_picks"), sort_keys=True)
                        if item.get("field_picks") is not None else None,
                        item["base_snapshot_id"],
                        item["dev_snapshot_id"],
                        item["main_snapshot_id"],
                    ),
                )
                saved += 1
        return saved

    def load_resolutions(self, plan_id: str) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM merge_resolutions WHERE plan_id = ? ORDER BY row_key",
                (plan_id,),
            ).fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            out[row["row_key"]] = {
                "decision": row["decision"],
                "action": row["action"],
                "field_picks": json.loads(row["field_picks_json"])
                if row["field_picks_json"] else None,
                "base_snapshot_id": row["base_snapshot_id"],
                "dev_snapshot_id": row["dev_snapshot_id"],
                "main_snapshot_id": row["main_snapshot_id"],
            }
        return out

    # ---- 读取 / 血缘 -------------------------------------------------------

    def get_commit(self, commit_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM commits WHERE commit_id = ?", (commit_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"commit {commit_id!r} not found",
                                    details={"commit_id": commit_id})
            merge = conn.execute(
                "SELECT * FROM merge_commits WHERE commit_id = ?", (commit_id,)
            ).fetchone()
        data = dict(row)
        if merge is not None:
            data["merge"] = {
                "base_commit_id": merge["base_commit_id"],
                "parent1_dev_id": merge["parent1_dev_id"],
                "parent2_main_id": merge["parent2_main_id"],
                "base_snapshot_id": merge["base_snapshot_id"],
                "parent1_snapshot_id": merge["parent1_snapshot_id"],
                "parent2_snapshot_id": merge["parent2_snapshot_id"],
                "resolution_summary": json.loads(merge["resolution_summary_json"]),
            }
            # 两条父引用对外统一暴露
            data["parent_commit_ids"] = [merge["parent1_dev_id"], merge["parent2_main_id"]]
        else:
            data["parent_commit_ids"] = (
                [row["parent_commit_id"]] if row["parent_commit_id"] else []
            )
        return data

    def get_branch(self, name: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM branches WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"branch {name!r} not found", details={"branch": name})
        return dict(row)

    def list_branches(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM branches ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def resolve_ref(self, ref: dict) -> tuple[str, str]:
        """把 {branch: x} 或 {commit_id: y} 解析为 (commit_id, snapshot_id)。"""
        if ref.get("branch"):
            branch = self.get_branch(ref["branch"])
            commit_id = branch["commit_id"]
        elif ref.get("commit_id"):
            commit_id = ref["commit_id"]
        else:
            from .errors import InvalidPayloadError
            raise InvalidPayloadError("exactly one of 'branch' or 'commit_id' is required")
        commit = self.get_commit(commit_id)
        return commit_id, commit["snapshot_id"]

    def ancestor_snapshots(self, commit_id: str) -> dict[str, str]:
        """commit_id -> snapshot_id 的全部祖先（含自身）映射，用于找共同祖先。"""
        out: dict[str, str] = {}
        with self._connect() as conn:
            frontier = [commit_id]
            while frontier:
                current = frontier.pop()
                if current in out:
                    continue
                row = conn.execute(
                    "SELECT snapshot_id, parent_commit_id FROM commits WHERE commit_id = ?",
                    (current,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"commit {current!r} not found",
                                        details={"commit_id": current})
                out[current] = row["snapshot_id"]
                if row["parent_commit_id"]:
                    frontier.append(row["parent_commit_id"])
                # 合并提交的另一条父边也要纳入祖先集
                merge = conn.execute(
                    "SELECT parent1_dev_id FROM merge_commits WHERE commit_id = ?",
                    (current,),
                ).fetchone()
                if merge is not None:
                    frontier.append(merge["parent1_dev_id"])
        return out

    def find_merge_base(self, commit_a: str, commit_b: str) -> dict:
        """找最近共同祖先提交（沿双亲边 BFS）。无共同祖先时抛 AncestryError。"""
        from .errors import AncestryError

        ancestors_a = self.ancestor_snapshots(commit_a)
        with self._connect() as conn:
            # BFS 自 commit_b 向上，第一个落在 ancestors_a 中的即最近共同祖先
            seen: set[str] = set()
            frontier = [commit_b]
            base_commit: str | None = None
            while frontier:
                current = frontier.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                if current in ancestors_a:
                    base_commit = current
                    break
                row = conn.execute(
                    "SELECT parent_commit_id FROM commits WHERE commit_id = ?", (current,)
                ).fetchone()
                if row is None:
                    raise AncestryError(
                        f"commit {current!r} not found while searching merge base",
                        details={"commit_id": current},
                    )
                parents: list[str] = []
                if row["parent_commit_id"]:
                    parents.append(row["parent_commit_id"])
                merge = conn.execute(
                    "SELECT parent1_dev_id FROM merge_commits WHERE commit_id = ?",
                    (current,),
                ).fetchone()
                if merge is not None:
                    parents.append(merge["parent1_dev_id"])
                frontier.extend(parents)
        if base_commit is None:
            raise AncestryError(
                f"no common ancestor between {commit_a} and {commit_b}",
                details={"commit_a": commit_a, "commit_b": commit_b},
            )
        base = self.get_commit(base_commit)
        return {"base_commit_id": base_commit, "base_snapshot_id": base["snapshot_id"]}

    # ---- 内部 --------------------------------------------------------------

    @staticmethod
    def _commit_exists(conn: sqlite3.Connection, commit_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM commits WHERE commit_id = ?", (commit_id,)
        ).fetchone() is not None

    @staticmethod
    def _require_commit_row(conn: sqlite3.Connection, commit_id: str) -> None:
        if not MetadataStore._commit_exists(conn, commit_id):
            raise NotFoundError(f"commit {commit_id!r} not found",
                                details={"commit_id": commit_id})

    @staticmethod
    def _require_snapshot_row(conn: sqlite3.Connection, snapshot_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"snapshot {snapshot_id!r} not found",
                                details={"snapshot_id": snapshot_id})
