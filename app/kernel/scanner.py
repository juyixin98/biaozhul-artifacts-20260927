"""读时应用扫描器（执行内核核心）。

删除应用顺序与语义（任何过滤、列裁剪之前完成，二者不能改变删除结果）：

1. 解析版本：按 snapshot_id 或 seq 定位快照，确定读取版本。
2. 取版本下“存活”数据文件（清单 ADD/DROP 仅追加；重写 = 新文件 ADD + 旧文件 DROP）。
3. 位置删除：按 (target_file_id, position) 精确命中。位置身份绑定到文件 id——
   文件被重写后产生新 id 与新内容哈希，旧位置删除不会作用到新文件（旧行号不复用）。
4. 等值删除：删除文件序列号 dseq 中每个非 NULL 键元组，只对 added_seq < dseq 的文件
   生效（严格小于：同快照/后来插入的同键行不得被删除）。
   键元组中任一列是 NULL，按 SQL 语义不匹配任何行，包括 NULL 数据行。
5. 之后才执行过滤；被删除行不参与过滤，也不可能因投影/过滤“复活”。

数据完整性：读取时校验文件 SHA-256 与行数；不匹配抛 COMPUTATION_FAILED。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.adapters import pyarrow_ops
from app.errors import ComputationFailed, NotFound, ValidationError
from app.kernel import filters as filter_dsl
from app.metadata.store import Store

KEPT = "KEPT"
DELETED = "DELETED"
FILTERED = "FILTERED"  # 仅 explain 使用：行存活但未通过过滤


@dataclass
class ScanResult:
    table_id: str
    snapshot_id: str
    seq: int
    rows: list[dict[str, Any]]
    data_files: list[str]
    delete_files: list[dict[str, Any]]
    null_keys_ignored: list[dict[str, Any]]
    intermediate: dict[str, Any] = field(default_factory=dict)


def _resolve_version(
    store: Store, conn, table_id: str, snapshot_id: str | None, seq: int | None
) -> Any:
    if snapshot_id is not None:
        snap = store.get_snapshot(conn, snapshot_id)
        if snap is None or snap["table_id"] != table_id:
            raise NotFound("SNAPSHOT_NOT_FOUND", "snapshot not found", {"snapshot_id": snapshot_id})
        return snap
    if seq is not None:
        snap = conn.execute(
            "SELECT * FROM snapshots WHERE table_id=? AND seq=?", (table_id, seq)
        ).fetchone()
        if snap is None:
            raise NotFound("SNAPSHOT_NOT_FOUND", "snapshot seq not found", {"seq": seq})
        return snap
    cur = store.current_snapshot(conn, table_id)
    if cur is None:
        raise NotFound("NO_SNAPSHOT", "table has no snapshot; nothing has been committed")
    return cur


def _filter_columns(node: dict[str, Any] | None, acc: set[str]) -> None:
    if not node:
        return
    if "and" in node:
        for c in node["and"]:
            _filter_columns(c, acc)
    elif "or" in node:
        for c in node["or"]:
            _filter_columns(c, acc)
    elif "not" in node:
        _filter_columns(node["not"], acc)
    else:
        acc.add(node["column"])


def run_scan(
    store: Store,
    *,
    table_id: str,
    snapshot_id: str | None = None,
    seq: int | None = None,
    columns: list[str] | None = None,
    filter: dict[str, Any] | None = None,
    include_filtered: bool = True,
    include_deleted: bool = True,
) -> ScanResult:
    config = store.config
    table = store.get_table(table_id)
    if table is None:
        raise NotFound("TABLE_NOT_FOUND", f"table '{table_id}' not found")
    schema_cols = json.loads(table["schema_json"])
    all_columns = [c["name"] for c in schema_cols]
    known = set(all_columns)

    if columns is not None:
        unknown = [c for c in columns if c not in known]
        if unknown:
            raise ValidationError("UNKNOWN_COLUMN", "unknown projected columns", {"columns": unknown})
    if filter is not None:
        filter_dsl.validate_filter_dsl(filter, known)

    with store.read_conn() as conn:
        snap = _resolve_version(store, conn, table_id, snapshot_id, seq)
        version_seq = snap["seq"]
        data_rows_meta = store.live_files_at(conn, table_id, version_seq)
        del_files = store.delete_files_for_scan(conn, table_id, version_seq)

        # ---- 读取删除向量 -------------------------------------------------
        position_marks: dict[str, dict[int, list[dict[str, Any]]]] = {}
        # file_id -> {position -> [reason, ...]}
        equality_sets: list[dict[str, Any]] = []
        null_keys_ignored: list[dict[str, Any]] = []
        delete_file_info: list[dict[str, Any]] = []

        for df in del_files:
            info = {
                "delete_file_id": df["delete_file_id"],
                "kind": df["kind"],
                "seq": df["seq"],
                "row_count": df["row_count"],
            }
            if df["kind"] == "POSITION":
                info["target_file_id"] = df["target_file_id"]
                pred_rows = pyarrow_ops.read_parquet(_abs(config, df["path"]))
                marks = position_marks.setdefault(df["target_file_id"], {})
                for r in pred_rows:
                    marks.setdefault(int(r["position"]), []).append(
                        {"kind": "POSITION", "delete_file_id": df["delete_file_id"], "seq": df["seq"]}
                    )
            else:
                key_cols = json.loads(df["key_columns"])
                info["key_columns"] = key_cols
                pred_rows = pyarrow_ops.read_parquet(_abs(config, df["path"]))
                match: dict[tuple, list[dict[str, Any]]] = {}
                null_count = 0
                for r in pred_rows:
                    tup = tuple(r[k] for k in key_cols)
                    if any(v is None for v in tup):
                        null_count += 1  # NULL 键不命中任何行
                        continue
                    match.setdefault(tup, []).append(
                        {
                            "kind": "EQUALITY",
                            "delete_file_id": df["delete_file_id"],
                            "seq": df["seq"],
                            "key": {k: r[k] for k in key_cols},
                        }
                    )
                equality_sets.append({"seq": df["seq"], "key_columns": key_cols, "match": match})
                if null_count:
                    null_keys_ignored.append(
                        {"delete_file_id": df["delete_file_id"], "seq": df["seq"], "count": null_count}
                    )
            delete_file_info.append(info)

        # ---- 列裁剪：投影 ∪ 过滤列 ∪ 等值键列（删除判定所需列始终读取） ----
        needed: set[str] = set(columns) if columns is not None else set(all_columns)
        needed |= {c for es in equality_sets for c in es["key_columns"]}
        _filter_columns(filter, needed)
        needed &= known
        read_cols = [c for c in all_columns if c in needed]
        out_cols = list(columns) if columns is not None else list(all_columns)

        # ---- 逐文件、逐行应用 ---------------------------------------------
        result_rows: list[dict[str, Any]] = []
        scanned_files: list[str] = []
        files_skipped_by_seq = 0
        for data_file in data_rows_meta:
            fid = data_file["file_id"]
            path = _abs(config, data_file["path"])
            _verify_content(data_file, path)
            scanned_files.append(fid)
            rows = pyarrow_ops.read_parquet(path, columns=read_cols)
            if len(rows) != data_file["row_count"]:
                raise ComputationFailed(
                    "ROW_COUNT_MISMATCH",
                    f"data file {fid} row count changed: metadata={data_file['row_count']} actual={len(rows)}",
                    {"file_id": fid},
                )
            marks = position_marks.get(fid, {})
            out_of_range = [p for p in marks if p >= len(rows)]
            if out_of_range:
                raise ComputationFailed(
                    "POSITION_OUT_OF_BOUNDS",
                    "position delete exceeds current file rows (metadata invariant broken)",
                    {"file_id": fid, "positions": sorted(out_of_range), "row_count": len(rows)},
                )
            applicable_equality = [es for es in equality_sets if data_file["added_seq"] < es["seq"]]
            if len(applicable_equality) < len(equality_sets):
                # 该文件的 added_seq 晚于部分删除文件序列号：那些删除对它不可见（先删后插保护）
                files_skipped_by_seq += 1

            for pos, row_full in enumerate(rows):
                reasons = list(marks.get(pos, []))
                for es in applicable_equality:
                    tup = tuple(row_full.get(k) for k in es["key_columns"])
                    if any(v is None for v in tup):
                        continue  # 数据键 NULL：不被等值删除命中
                    hit = es["match"].get(tup)
                    if hit:
                        reasons.extend(hit)
                if reasons:
                    if not include_deleted:
                        continue
                    disposition = DELETED
                elif filter is not None and not filter_dsl.evaluate(filter, row_full):
                    if not include_filtered:
                        continue
                    disposition = FILTERED
                else:
                    disposition = KEPT
                result_rows.append(
                    {
                        "file_id": fid,
                        "position": pos,
                        "added_seq": data_file["added_seq"],
                        "disposition": disposition,
                        "reasons": reasons,
                        "row": {k: row_full.get(k) for k in out_cols},
                    }
                )

    return ScanResult(
        table_id=table_id,
        snapshot_id=snap["snapshot_id"],
        seq=version_seq,
        rows=result_rows,
        data_files=scanned_files,
        delete_files=delete_file_info,
        null_keys_ignored=null_keys_ignored,
        intermediate={
            "read_columns": read_cols,
            "projected_columns": out_cols,
            "live_file_count": len(data_rows_meta),
            "visible_delete_file_count": len(del_files),
            "equality_set_count": len(equality_sets),
            "position_target_count": len(position_marks),
            "files_with_partial_equality_window": files_skipped_by_seq,
        },
    )


def _abs(config, rel_path: str):
    from pathlib import Path

    p = Path(rel_path)
    return p if p.is_absolute() else config.warehouse_dir / p


def _verify_content(data_file, path) -> None:
    actual = pyarrow_ops.sha256_file(path)
    if actual != data_file["content_hash"]:
        raise ComputationFailed(
            "CONTENT_HASH_MISMATCH",
            f"content of file {data_file['file_id']} differs from committed identity",
            {"file_id": data_file["file_id"], "expected": data_file["content_hash"], "actual": actual},
        )
