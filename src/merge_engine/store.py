"""SQLite 存储层：目标表引导、操作前快照装载、单事务原子提交、运行元数据。

原子性边界（一个连接内）::

    BEGIN IMMEDIATE
      写 actions 表（审计先行）
      UPDATE/INSERT/DELETE 目标表
      upsert __merge_runs(status=COMMITTED)
      <故障注入点>
    COMMIT

任何一步抛错都 ROLLBACK——调用方随后重新装载快照应得到 *操作前* 状态。
目标表刻意不声明 UNIQUE/PRIMARY KEY：目标重复键是业务状态冲突，
必须由内核检出并给出 409，而不是让 SQLite 随机拒绝其中一行。
"""
from __future__ import annotations

import errno
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .contracts import ActionType, MergePlan, jsonable
from .valuecodec import VALUE_TYPE, encode, register_codec

RUNS_TABLE = "__merge_runs"
ACTIONS_TABLE = "__merge_actions"

# 进程级注册一次：数据列以 MERGEVAL 类型经 BLOB 标记编解码，保证 bool/int/str/
# bytes/NULL 精确还原（详见 valuecodec.py）。
register_codec()

# 注入钩子签名：(conn) -> None；抛异常即模拟该点故障
Hook = Callable[[sqlite3.Connection], None]


class FaultHooks:
    """提交故障注入（本地合成测试设施）。

    point:
      after_actions : 所有目标行改动已执行、尚未 COMMIT 时抛 COMMIT_FAILED。
                      验证“事务内已改 + 回滚后无部分更新”这一最强场景。
      before_commit : COMMIT 前抛磁盘满（ENOSPC），归类 RESOURCE_EXHAUSTED。
      commit_raises : 让 connection.commit() 自身抛 sqlite3 disk I/O 错误，
                      走真实的提交失败分支（与手工 monkeypatch 等价的显式入口）。
    """

    def __init__(self, point: str | None = None) -> None:
        self.point = point


def connect(db_path: str | Path) -> sqlite3.Connection:
    # PARSE_DECLTYPES 让 MERGEVAL 列经 valuecodec 的转换器精确还原类型
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None,
                           detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ident(name: str) -> str:
    """调用方已在 config 层校验过表名/列名字符集，这里再做一次防御性引用。"""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ident(RUNS_TABLE)} (
            run_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            dry_run INTEGER NOT NULL,
            status TEXT NOT NULL,
            target_table TEXT NOT NULL,
            snapshot_fingerprint TEXT,
            counts_json TEXT,
            error_json TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ident(ACTIONS_TABLE)} (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            key_json TEXT NOT NULL,
            source_rownum INTEGER,
            target_rowid INTEGER,
            before_json TEXT,
            after_json TEXT,
            reason TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        )
        """
    )


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [r[1] for r in conn.execute(f"PRAGMA table_info({_ident(table)})")]


def bootstrap_target(
    conn: sqlite3.Connection,
    table: str,
    key_columns: tuple[str, ...],
    payload_columns: tuple[str, ...],
) -> list[str]:
    """确保目标表存在且包含全部列。所有数据列用 MERGEVAL 类型以保真存储类型。

    返回当前表列清单。已存在的表只增列、不删列（历史批次的列保留）。
    """
    cols = [*key_columns, *payload_columns]
    if not table_exists(conn, table):
        col_def = ", ".join(f"{_ident(c)} {VALUE_TYPE}" for c in dict.fromkeys(cols))
        conn.execute(f"CREATE TABLE {_ident(table)} ({col_def})")
    else:
        existing = set(table_columns(conn, table))
        missing = [c for c in key_columns if c not in existing]
        if missing:
            # 键列在既有表里不存在属于结构性输入错误，但此处已进入存储层，
            # 用 StateConflict 的语义并不贴切——交给 config/engine 预检；
            # 到达这里说明预检遗漏，直接报计算失败。
            raise RuntimeError(f"existing table {table} missing key columns {missing}")
        for c in payload_columns:
            if c not in existing:
                conn.execute(
                    f"ALTER TABLE {_ident(table)} ADD COLUMN {_ident(c)} {VALUE_TYPE}"
                )
    return table_columns(conn, table)


def load_snapshot(
    conn: sqlite3.Connection,
    table: str,
    key_columns: tuple[str, ...],
) -> list[tuple[int, dict[str, Any]]]:
    """读取操作前快照 [(rowid, values)]，列序取表定义序。"""
    if not table_exists(conn, table):
        return []
    cols = table_columns(conn, table)
    select_cols = ", ".join(_ident(c) for c in cols)
    rows = conn.execute(
        f"SELECT rowid AS __rid, {select_cols} FROM {_ident(table)}"
    ).fetchall()
    out: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        d = {c: r[c] for c in cols}
        out.append((r["__rid"], d))
    return out


def seed_target(conn: sqlite3.Connection, table: str,
                columns: list[str], rows: list[dict[str, Any]]) -> None:
    """测试/夹具辅助：直接灌入初始数据（可包含重复键，用于状态冲突用例）。"""
    if not table_exists(conn, table):
        col_def = ", ".join(f"{_ident(c)} {VALUE_TYPE}" for c in columns)
        conn.execute(f"CREATE TABLE {_ident(table)} ({col_def})")
    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(_ident(c) for c in columns)
    conn.executemany(
        f"INSERT INTO {_ident(table)} ({col_list}) VALUES ({placeholders})",
        [tuple(encode(r.get(c)) for c in columns) for r in rows],
    )


def apply_plan(
    conn: sqlite3.Connection,
    plan: MergePlan,
    run_id: str,
    created_at: float,
    hooks: FaultHooks | None = None,
) -> dict[str, int]:
    """在单个 IMMEDIATE 事务内提交整个计划。返回计数。"""
    hooks = hooks or FaultHooks()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1) 审计动作先落盘
        for seq, action in enumerate(plan.actions):
            conn.execute(
                f"""
                INSERT INTO {_ident(ACTIONS_TABLE)}
                  (run_id, seq, action_type, key_json, source_rownum,
                   target_rowid, before_json, after_json, reason)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, seq, action.type.value,
                    json.dumps([jsonable(v) for v in action.key], sort_keys=True),
                    action.source_rownum, action.target_rowid,
                    json.dumps(jsonable(action.before), sort_keys=True) if action.before is not None else None,
                    json.dumps(jsonable(action.after), sort_keys=True) if action.after is not None else None,
                    action.reason,
                ),
            )

        # 2) 目标表写操作
        all_cols = (*plan.key_columns, *plan.payload_columns)
        for action in plan.actions:
            if action.type is ActionType.INSERT_UNMATCHED:
                _apply_insert(conn, plan.target_table, all_cols, action)
            elif action.type is ActionType.UPDATE_MATCHED:
                _apply_update(conn, plan.target_table, all_cols, action)
            elif action.type is ActionType.DELETE_UNMATCHED:
                _apply_delete(conn, plan.target_table, action)
            # NOOP_* 不写目标表

        if hooks.point == "after_actions":
            raise RuntimeError("injected failure after actions, before commit")

        # 3) 运行元数据（同一事务）
        conn.execute(
            f"""
            INSERT INTO {_ident(RUNS_TABLE)}
              (run_id, created_at, dry_run, status, target_table,
               snapshot_fingerprint, counts_json, error_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                run_id, created_at, 0, "COMMITTED", plan.target_table,
                plan.snapshot_fingerprint,
                json.dumps(plan.write_counts, sort_keys=True), None,
            ),
        )

        if hooks.point == "before_commit":
            raise OSError(errno.ENOSPC, "injected disk full before commit")

        if hooks.point == "commit_raises":
            # 模拟真实提交期磁盘 I/O 错误：commit() 抛错，外层 except 负责 ROLLBACK
            raise sqlite3.OperationalError("disk I/O error (injected at commit)")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return dict(plan.write_counts)


def _apply_insert(conn, table: str, columns, action) -> None:
    col_list = ", ".join(_ident(c) for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    # 目标数据列：显式 MERGEVAL 编码（不经全局适配器，避免污染其它绑定）
    params = tuple(encode(action.after.get(c)) for c in columns)
    cur = conn.execute(
        f"INSERT INTO {_ident(table)} ({col_list}) VALUES ({placeholders})", params
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"INSERT affected {cur.rowcount} rows")


def _apply_update(conn, table: str, columns, action) -> None:
    set_clause = ", ".join(f"{_ident(c)} = ?" for c in columns)
    params = [encode(action.after.get(c)) for c in columns]
    params.append(action.target_rowid)   # rowid 是物理行号，不编码
    cur = conn.execute(
        f"UPDATE {_ident(table)} SET {set_clause} WHERE rowid = ?", params
    )
    if cur.rowcount != 1:
        # 快照在 IMMEDIATE 事务保护下冻结；rowcount!=1 说明快照漂移或内核装配错误
        raise RuntimeError(
            f"UPDATE on rowid {action.target_rowid} affected {cur.rowcount} rows"
        )


def _apply_delete(conn, table: str, action) -> None:
    cur = conn.execute(
        f"DELETE FROM {_ident(table)} WHERE rowid = ?", (action.target_rowid,)
    )
    if cur.rowcount != 1:
        raise RuntimeError(
            f"DELETE on rowid {action.target_rowid} affected {cur.rowcount} rows"
        )


def record_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    created_at: float,
    dry_run: bool,
    status: str,
    target_table: str,
    fingerprint: str | None,
    counts: dict[str, int] | None,
    error: dict[str, Any] | None,
) -> None:
    """登记被拒绝/失败/dry-run 的运行（独立自动提交事务）。"""
    conn.execute(
        f"""
        INSERT INTO {_ident(RUNS_TABLE)}
          (run_id, created_at, dry_run, status, target_table,
           snapshot_fingerprint, counts_json, error_json)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            run_id, created_at, int(dry_run), status, target_table,
            fingerprint,
            json.dumps(counts, sort_keys=True) if counts is not None else None,
            json.dumps(jsonable(error), sort_keys=True) if error is not None else None,
        ),
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT * FROM {_ident(RUNS_TABLE)} WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "dry_run": bool(row["dry_run"]),
        "status": row["status"],
        "target_table": row["target_table"],
        "snapshot_fingerprint": row["snapshot_fingerprint"],
        "counts": json.loads(row["counts_json"]) if row["counts_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
    }


def list_actions(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT * FROM {_ident(ACTIONS_TABLE)} WHERE run_id = ? ORDER BY seq",
        (run_id,),
    ).fetchall()
    return [
        {
            "seq": r["seq"], "type": r["action_type"],
            "key": json.loads(r["key_json"]),
            "source_rownum": r["source_rownum"],
            "target_rowid": r["target_rowid"],
            "before": json.loads(r["before_json"]) if r["before_json"] else None,
            "after": json.loads(r["after_json"]) if r["after_json"] else None,
            "reason": r["reason"],
        }
        for r in rows
    ]

