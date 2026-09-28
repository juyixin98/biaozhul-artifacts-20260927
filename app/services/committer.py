"""提交执行器：plan 通过后落物理文件，并在单个元数据事务内发布快照。

失败语义：
- 物理文件先写临时目录风格（adapter 内部 .tmp 原子替换）；任何文件写失败都不会留下元数据。
- 元数据事务失败时回滚，并清理本次已落盘的物理文件。
- seq/snapshot_id/file_id 在事务内确定；并发提交由父快照校验 + BEGIN IMMEDIATE 串行化。
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.adapters import pyarrow_ops
from app.metadata.store import Store
from app.services.planner import (
    Plan,
    PlannedAppend,
    PlannedEqualityDelete,
    PlannedPositionDelete,
    plan_commit,
)


def commit(store: Store, *, run_id: str, table_id: str,
           parent_snapshot_id: str | None, operations: list[dict[str, Any]]) -> dict[str, Any]:
    plan = plan_commit(
        store, table_id=table_id, parent_snapshot_id=parent_snapshot_id, operations=operations
    )
    config = store.config

    written_data: list[tuple[str, Path]] = []
    written_deletes: list[tuple[str, Path]] = []
    try:
        # ---- 1) 物理文件全部落盘（此时元数据尚不可见） --------------------
        for a in plan.appends:
            a.file_id = _new_id("file")
            plan.refs[a.ref] = a.file_id
            path = config.data_path(table_id, a.file_id)
            h = pyarrow_ops.write_data_file(path, a.rows, plan.schema_columns)
            written_data.append((a.file_id, path))
            plan.log("data_file_written", {"ref": a.ref, "file_id": a.file_id,
                                           "path": str(path), "rows": len(a.rows), "sha256": h})

        for pd in plan.position_deletes:
            pd.delete_file_id = _new_id("pdel")
            path = config.delete_path(table_id, pd.delete_file_id)
            h = pyarrow_ops.write_position_delete_file(path, pd.target_file_id, pd.positions)
            written_deletes.append((pd.delete_file_id, path))
            plan.log("position_delete_written", {
                "delete_file_id": pd.delete_file_id, "target_file_id": pd.target_file_id,
                "rows": len(pd.positions), "sha256": h,
            })

        for ed in plan.equality_deletes:
            ed.delete_file_id = _new_id("edel")
            path = config.delete_path(table_id, ed.delete_file_id)
            h = pyarrow_ops.write_equality_delete_file(
                path, plan.primary_key, plan.schema_columns, ed.predicates
            )
            written_deletes.append((ed.delete_file_id, path))
            plan.log("equality_delete_written", {
                "delete_file_id": ed.delete_file_id, "key_columns": plan.primary_key,
                "rows": len(ed.predicates), "sha256": h,
            })

        # ---- 2) 单事务发布元数据 -----------------------------------------
        with store.transaction() as conn:
            current = store.current_snapshot(conn, table_id)
            # 事务内复核父快照（plan 与本事务之间可能有其他提交落地）
            expected = None if current is None else current["snapshot_id"]
            if expected != plan.parent_snapshot_id:
                from app.errors import StateConflict

                raise StateConflict(
                    "PARENT_MISMATCH",
                    "parent snapshot changed between planning and commit",
                    {"given": plan.parent_snapshot_id, "expected": expected},
                )
            seq = 1 if current is None else current["seq"] + 1
            snapshot_id = _new_id("snap")

            for a in plan.appends:
                rel = _rel(config, config.data_path(table_id, a.file_id))
                store.insert_data_file(
                    conn, file_id=a.file_id, table_id=table_id, path=rel,
                    content_hash=pyarrow_ops.sha256_file(config.data_path(table_id, a.file_id)),
                    row_count=len(a.rows), added_seq=seq,
                )
                store.add_manifest(conn, table_id=table_id, file_id=a.file_id, seq=seq,
                                   snapshot_id=snapshot_id, change="ADD", reason="COMMIT")
                for drop in a.drops:
                    store.add_manifest(conn, table_id=table_id, file_id=drop, seq=seq,
                                       snapshot_id=snapshot_id, change="DROP", reason="REWRITE")
                store.insert_event(
                    conn, run_id=run_id, table_id=table_id, snapshot_id=snapshot_id, seq=seq,
                    event_type="DATA_FILE_ADDED" if a.kind == "append" else "DATA_FILE_REWRITTEN",
                    payload={"ref": a.ref, "file_id": a.file_id, "rows": len(a.rows),
                             "drops": a.drops},
                )

            for pd in plan.position_deletes:
                rel = _rel(config, config.delete_path(table_id, pd.delete_file_id))
                store.insert_delete_file(
                    conn, delete_file_id=pd.delete_file_id, table_id=table_id, path=rel,
                    content_hash=pyarrow_ops.sha256_file(config.delete_path(table_id, pd.delete_file_id)),
                    kind="POSITION", target_file_id=pd.target_file_id, key_columns=None,
                    row_count=len(pd.positions), seq=seq, snapshot_id=snapshot_id,
                )
                store.insert_event(
                    conn, run_id=run_id, table_id=table_id, snapshot_id=snapshot_id, seq=seq,
                    event_type="POSITION_DELETE_ADDED",
                    payload={"delete_file_id": pd.delete_file_id,
                             "target_file_id": pd.target_file_id, "positions": pd.positions},
                )

            for ed in plan.equality_deletes:
                rel = _rel(config, config.delete_path(table_id, ed.delete_file_id))
                store.insert_delete_file(
                    conn, delete_file_id=ed.delete_file_id, table_id=table_id, path=rel,
                    content_hash=pyarrow_ops.sha256_file(config.delete_path(table_id, ed.delete_file_id)),
                    kind="EQUALITY", target_file_id=None, key_columns=plan.primary_key,
                    row_count=len(ed.predicates), seq=seq, snapshot_id=snapshot_id,
                )
                null_preds = [p for p in ed.predicates if any(v is None for v in p["key"].values())]
                store.insert_event(
                    conn, run_id=run_id, table_id=table_id, snapshot_id=snapshot_id, seq=seq,
                    event_type="EQUALITY_DELETE_ADDED",
                    payload={"delete_file_id": ed.delete_file_id, "key_columns": plan.primary_key,
                             "predicate_count": len(ed.predicates),
                             "null_key_predicates": len(null_preds)},
                )

            summary = {
                "new_data_files": len(plan.appends),
                "rewritten_files": sum(len(a.drops) for a in plan.appends),
                "position_delete_files": len(plan.position_deletes),
                "equality_delete_files": len(plan.equality_deletes),
            }
            store.insert_snapshot(conn, snapshot_id=snapshot_id, table_id=table_id, seq=seq,
                                  parent_id=plan.parent_snapshot_id, summary=summary)
            store.insert_event(
                conn, run_id=run_id, table_id=table_id, snapshot_id=snapshot_id, seq=seq,
                event_type="SNAPSHOT_COMMITTED", payload={"summary": summary, "parent": plan.parent_snapshot_id},
            )

        plan.log("metadata_committed", {"snapshot_id": snapshot_id, "seq": seq})
        return {
            "snapshot_id": snapshot_id,
            "seq": seq,
            "parent_snapshot_id": plan.parent_snapshot_id,
            "summary": summary,
            "files": [{"ref": a.ref, "file_id": a.file_id, "rows": len(a.rows), "drops": a.drops}
                      for a in plan.appends],
            "delete_files": [
                {"delete_file_id": pd.delete_file_id, "kind": "POSITION",
                 "target_file_id": pd.target_file_id, "rows": len(pd.positions)}
                for pd in plan.position_deletes
            ] + [
                {"delete_file_id": ed.delete_file_id, "kind": "EQUALITY",
                 "key_columns": plan.primary_key, "rows": len(ed.predicates)}
                for ed in plan.equality_deletes
            ],
            "phases": plan.phases,
        }
    except BaseException:
        # 元数据未发布时，清理本次已写物理文件（文件不可变且无元数据引用）
        for _, p in written_data + written_deletes:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def _rel(config, path: Path) -> str:
    try:
        return str(path.relative_to(config.warehouse_dir))
    except ValueError:
        return str(path)
