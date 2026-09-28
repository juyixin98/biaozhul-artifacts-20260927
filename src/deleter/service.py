"""服务层：编排元数据事务、格式适配、执行内核与血缘侧录。

事务边界：
* load / delete / rewrite 各自是一个 sqlite 事务；
* 序列号分配与元数据写入同事务提交；
* Parquet 新文件先写临时路径、提交成功后原子改名；回滚不留半成品。

服务层不感知 HTTP；API 层只做 schema 校验与错误翻译。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from .adapters import parquet_format as pf
from .adapters.ingestion import IngestionService
from .adapters.lineage import LineageStore
from .errors import ComputeFailureError, InputError, NotFoundError, StateConflictError
from .kernel import models as km
from .kernel.executor import evaluate_table
from .metadata.store import MetadataStore
from .observability.run_logger import RunLogger, utc_now_iso

FILTER_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "is_null", "not_null"}


class DeleterService:
    def __init__(
        self,
        workspace: str | os.PathLike[str],
        max_rows_per_load: int = 200_000,
        run_logger: RunLogger | None = None,
    ) -> None:
        self.root = Path(workspace)
        self.data_dir = self.root / "data"
        self.tables_dir = self.data_dir / "tables"
        self.inbox_dir = self.root / "inbox"
        for d in (self.data_dir, self.tables_dir, self.inbox_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.store = MetadataStore(self.data_dir / "meta.sqlite3")
        self.lineage_root = self.data_dir / "lineage"
        self.lineage_root.mkdir(parents=True, exist_ok=True)
        self._lineage_cache: dict[str, LineageStore] = {}
        self.ingestion = IngestionService(self.inbox_dir, max_rows_per_load)
        self.run_logger = run_logger or RunLogger(self.root)
        self._cleanup_staging()

    def _cleanup_staging(self) -> None:
        for tmp in self.tables_dir.rglob("*.tmp"):
            tmp.unlink(missing_ok=True)

    def _lineage(self, table_id: str) -> LineageStore:
        store = self._lineage_cache.get(table_id)
        if store is None:
            store = LineageStore(self.lineage_root / table_id)
            self._lineage_cache[table_id] = store
        return store

    def close(self) -> None:
        self.store.close()

    # ================= 表管理 =================
    def create_table(self, table_id: str, columns: dict[str, str], key_columns: list[str]) -> dict[str, Any]:
        unknown = [c for c in key_columns if c not in columns]
        if unknown:
            raise InputError("主键列不在 schema 中", unknown=unknown)
        if not key_columns:
            raise InputError("至少需要一个主键列")
        if len(set(key_columns)) != len(key_columns):
            raise InputError("主键列不可重复")
        for name in columns:
            pf.resolve_type(columns[name])  # 提前拒绝不支持类型
        (self.tables_dir / table_id).mkdir(parents=True, exist_ok=True)
        self.store.create_table(table_id, dict(columns), list(key_columns), utc_now_iso())
        return self.describe_table(table_id)

    def describe_table(self, table_id: str) -> dict[str, Any]:
        table = self.store.get_table(table_id)
        live = self.store.list_files(table_id, live_only=True)
        table["live_files"] = [f["file_id"] for f in live]
        table["seq_horizon"] = table["next_seq"] - 1
        return table

    # ================= 载入（插入） =================
    def load_file(self, table_id: str, file_id: str, source: dict[str, Any]) -> dict[str, Any]:
        table = self.store.get_table(table_id)
        if self.store.file_exists_any_version(table_id, file_id):
            raise StateConflictError(
                "file_id 已存在；载入是不可变操作，请使用新 file_id", file_id=file_id,
            )
        rows = self.ingestion.load_rows(source)
        self.ingestion.validate_against_schema(rows, table["columns"])
        arrow_table = pf.pylist_to_table(rows, table["columns"])
        fingerprint = pf.content_fingerprint(rows)

        with self.store.txn() as conn:
            seq = self.store.allocate_seq(conn, table_id)  # 本批插入序列号
            target = self._parquet_path(table_id, file_id, 1)
            pf.write_parquet(arrow_table, target)
            self.store.insert_file(
                conn, table_id=table_id, file_id=file_id, version=1,
                row_count=len(rows), fingerprint=fingerprint, created_seq=seq, ts=utc_now_iso(),
            )
        # 侧录：所有行以同一批 seq 进入
        self._lineage(table_id).write(file_id, 1, [seq] * len(rows),
                           {"kind": "load", "seq": seq})
        return {
            "table_id": table_id, "file_id": file_id, "version": 1,
            "rows_loaded": len(rows), "insert_seq": seq,
            "fingerprint": fingerprint,
        }

    # ================= 删除注册 =================
    def apply_deletes(
        self, table_id: str, requests: list[dict[str, Any]],
        trace: Callable[[dict], None] | None = None,
    ) -> dict[str, Any]:
        """注册一批删除并立即评估。每个删除占一个序列号（按请求顺序）。"""
        if not requests:
            raise InputError("删除请求列表为空")
        self.store.get_table(table_id)
        results: list[dict[str, Any]] = []
        with self.store.txn() as conn:
            for req in requests:
                results.append(self._register_one(conn, table_id, req))
        # 提交后做一次评估，回传命中情况
        report = self.evaluate_current(table_id, trace=trace)
        by_id = {e.delete_id: e for e in report.op_evaluations}
        for r in results:
            if r.get("idempotent"):
                # 幂等重试：保留原 seq 与 idempotent 标记，不重算状态
                continue
            ev = by_id.get(r["delete_id"])
            r["status"] = ev.status if ev else r["status"]
            r["matched_rows"] = [list(x) for x in ev.matched_rows] if ev else []
        return {"results": results, "report": self._report_summary(report)}

    def _register_one(self, conn, table_id: str, req: dict[str, Any]) -> dict[str, Any]:
        delete_id = req.get("delete_id")
        kind = req.get("kind")
        if not isinstance(delete_id, str) or not delete_id:
            raise InputError("delete_id 必须是非空字符串")

        existing = self.store.get_delete(table_id, delete_id)
        if existing is not None:
            # 幂等：同一 delete_id 必须携带完全相同的删除规格
            if _spec_payload(kind, req) != existing["spec"]:
                raise StateConflictError(
                    "delete_id 已存在但删除规格不同",
                    delete_id=delete_id,
                    existing_spec=existing["spec"],
                    given_spec=_spec_payload(kind, req),
                )
            return {"delete_id": delete_id, "kind": existing["kind"],
                    "seq": existing["seq"], "status": km.OP_IDEMPOTENT,
                    "bound_version": existing["bound_version"], "idempotent": True}

        if kind == km.POSITION:
            return self._register_position(conn, table_id, delete_id, req)
        if kind == km.EQUALITY:
            return self._register_equality(conn, table_id, delete_id, req)
        raise InputError("kind 必须是 position 或 equality", got=kind)

    def _register_position(self, conn, table_id, delete_id, req) -> dict[str, Any]:
        file_id = req.get("file_id")
        rn = req.get("row_number")
        if not isinstance(file_id, str) or not file_id:
            raise InputError("position 删除要求 file_id")
        if not isinstance(rn, int) or isinstance(rn, bool) or rn < 0:
            raise InputError("row_number 必须是 >=0 的整数（0 基）")
        file_meta = self.store.get_file(table_id, file_id)  # 最新版本
        if not file_meta["is_live"]:
            # 整个文件已被重写合并：删除无法绑定当前内容
            raise StateConflictError(
                "目标文件当前不是 live 版本（已被重写）",
                file_id=file_id, latest_version=file_meta["version"],
            )
        if rn >= file_meta["row_count"]:
            raise InputError(
                "row_number 超出文件当前行数",
                row_number=rn, row_count=file_meta["row_count"],
            )
        seq = self.store.allocate_seq(conn, table_id)
        spec = _spec_payload(km.POSITION, req)
        self.store.insert_delete(
            conn, table_id=table_id, delete_id=delete_id, kind=km.POSITION, seq=seq,
            file_id=file_id, row_number=rn, bound_version=file_meta["version"],
            key_cols=None, key_vals=None, spec=spec, ts=utc_now_iso(),
        )
        return {"delete_id": delete_id, "kind": km.POSITION, "seq": seq,
                "status": km.OP_APPLIED, "bound_version": file_meta["version"],
                "idempotent": False}

    def _register_equality(self, conn, table_id, delete_id, req) -> dict[str, Any]:
        table = self.store.get_table(table_id)
        key_cols: list[str] = table["key_columns"]
        key = req.get("key")
        if not isinstance(key, dict):
            raise InputError("equality 删除要求 key 为对象，给出全部主键列的等值谓词")
        missing = [c for c in key_cols if c not in key]
        extra = [c for c in key if c not in key_cols]
        if missing or extra:
            raise InputError("key 必须且只能包含全部主键列", missing=missing, extra=extra)
        vals = [key[c] for c in key_cols]
        _check_predicate_types(vals, key_cols, table["columns"])
        seq = self.store.allocate_seq(conn, table_id)
        spec = _spec_payload(km.EQUALITY, req)
        self.store.insert_delete(
            conn, table_id=table_id, delete_id=delete_id, kind=km.EQUALITY, seq=seq,
            file_id=None, row_number=None, bound_version=None,
            key_cols=key_cols, key_vals=vals, spec=spec, ts=utc_now_iso(),
        )
        return {"delete_id": delete_id, "kind": km.EQUALITY, "seq": seq,
                "status": km.OP_APPLIED_ZERO, "bound_version": None, "idempotent": False}

    # ================= 读时应用 =================
    def _build_file_data(self, table_id: str) -> list[km.FileData]:
        out: list[km.FileData] = []
        for f in self.store.list_files(table_id, live_only=True):
            rows_py = pf.table_to_pylist(pf.read_parquet(
                self._parquet_path(table_id, f["file_id"], f["version"])))
            seqs = self._lineage(table_id).insert_seqs(f["file_id"], f["version"])
            if len(seqs) != len(rows_py):
                raise ComputeFailureError(
                    "血缘与数据行数不一致", file_id=f["file_id"],
                    data_rows=len(rows_py), lineage_rows=len(seqs),
                )
            rows = [km.Row(values=r, insert_seq=s) for r, s in zip(rows_py, seqs)]
            out.append(km.FileData(
                file_id=f["file_id"], version=f["version"], rows=rows,
                columns=tuple(rows_py[0].keys()) if rows_py else tuple(),
            ))
        return out

    def _build_ops(self, table_id: str) -> list[km.DeleteOp]:
        out = []
        for d in self.store.list_deletes(table_id):
            out.append(km.DeleteOp(
                delete_id=d["delete_id"], kind=d["kind"], seq=d["seq"],
                file_id=d["file_id"], row_number=d["row_number"],
                bound_version=d["bound_version"],
                key_columns=tuple(d["key_columns"] or ()),
                key_values=tuple(d["key_values"] or ()),
            ))
        return out

    def _bound_row_counts(self, table_id: str) -> dict[tuple[str, int], int]:
        counts: dict[tuple[str, int], int] = {}
        for d in self.store.list_deletes(table_id):
            if d["kind"] == km.POSITION:
                try:
                    f = self.store.get_file(table_id, d["file_id"], d["bound_version"])
                    counts[(d["file_id"], d["bound_version"])] = f["row_count"]
                except NotFoundError:
                    pass
        return counts

    def _survived_coords(self, table_id: str) -> frozenset[tuple[str, int, int]]:
        """所有重写产物血缘中（沿血缘链传递）出现过的父坐标。

        含义：(fid, ver, rn) 出现过 -> 该行曾作为幸存内容被某次重写携带，
        绑定它的旧位置删除因内容身份更替而失效（stale_file_rewritten）。
        未出现 -> 该行在使文件退出 live 的重写之前就已被删除
        （stale_row_already_removed）。链条式重写时逐级向上展开，使中间
        重写产物里的行坐标也被计入（与行 insert_seq 延续同理）。
        """
        lineage_dir = self.lineage_root / table_id
        # 先收集每个 (file, version) 的直接父坐标
        direct: dict[tuple[str, int], list[tuple[str, int, int]]] = {}
        for path in sorted(lineage_dir.glob("*.v*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if rec.get("source", {}).get("kind") != "rewrite":
                continue
            key = (rec["file_id"], rec["version"])
            direct[key] = [tuple(p) for p in rec["source"]["parents"]]

        coords: set[tuple[str, int, int]] = set()

        def expand(file_id: str, version: int) -> None:
            for pid, pver, prn in direct.get((file_id, version), []):
                coords.add((pid, pver, prn))
                if (pid, pver) in direct:
                    expand(pid, pver)  # 父行本身也来自更早重写，继续回溯

        for key in direct:
            expand(*key)
        return frozenset(coords)

    def evaluate_current(
        self, table_id: str, trace: Callable[[dict], None] | None = None,
    ) -> km.ScanReport:
        table = self.store.get_table(table_id)
        files = self._build_file_data(table_id)
        ops = self._build_ops(table_id)
        report = evaluate_table(
            files, ops, table["key_columns"],
            bound_row_counts=self._bound_row_counts(table_id),
            survived_coords=self._survived_coords(table_id),
            seq_horizon=table["next_seq"] - 1, trace=trace,
        )
        report.table_id = table_id
        return report

    # ================= 物理重写 =================
    def rewrite_files(self, table_id: str, file_ids: list[str], new_file_id: str) -> dict[str, Any]:
        """把指定 live 文件的幸存行物理重写为一个新内容版本。

        * 不分配新序列号、不改变 insert_seq（行身份延续）；
        * 旧 file_id/version 标记 superseded；旧位置删除自动失去绑定；
        * 幸存行在新文件中保持相对行号（压缩前缀），但旧行号删除绝不复用。
        """
        table = self.store.get_table(table_id)
        if not file_ids:
            raise InputError("file_ids 为空")
        if len(set(file_ids)) != len(file_ids):
            raise InputError("file_ids 不可重复")
        live = {f["file_id"]: f for f in self.store.list_files(table_id, live_only=True)}
        for fid in file_ids:
            if fid not in live:
                raise NotFoundError("文件不存在或当前不是 live", file_id=fid)
        if self.store.file_exists_any_version(table_id, new_file_id):
            raise StateConflictError("new_file_id 已被占用", new_file_id=new_file_id)

        # 1) 计算当前幸存行（在事务外做读计算；写阶段重新取版本做冲突检查）
        report = self.evaluate_current(table_id)
        kept_by_file: dict[str, list[km.RowVerdict]] = {fid: [] for fid in file_ids}
        for v in report.kept_rows():
            if v.file_id in kept_by_file:
                kept_by_file[v.file_id].append(v)

        # 2) 取旧行数据与血缘，组装新行
        merged_rows: list[dict[str, Any]] = []
        merged_seqs: list[int] = []
        parents: list[list[Any]] = []
        old_rows_cache: dict[str, list[dict[str, Any]]] = {}
        old_seq_cache: dict[str, list[int]] = {}
        for fid in file_ids:
            ver = live[fid]["version"]
            old_rows = pf.table_to_pylist(pf.read_parquet(self._parquet_path(table_id, fid, ver)))
            old_seqs = self._lineage(table_id).insert_seqs(fid, ver)
            old_rows_cache[fid] = old_rows
            old_seq_cache[fid] = old_seqs
            kept_rns = {v.row_number for v in kept_by_file[fid]}
            for rn in sorted(kept_rns):
                merged_rows.append(old_rows[rn])
                merged_seqs.append(old_seqs[rn])
                parents.append([fid, ver, rn])

        if not merged_rows:
            raise StateConflictError("重写结果为空（全部行已删除）；拒绝产生空文件版本")

        arrow_table = pf.pylist_to_table(merged_rows, table["columns"])
        fingerprint = pf.content_fingerprint(merged_rows)

        # 3) 事务：版本复核 -> 写文件 -> 元数据切换
        old_keys = [(fid, live[fid]["version"]) for fid in file_ids]
        with self.store.txn() as conn:
            # 重新确认文件版本未被并发改写
            for fid in file_ids:
                cur = self.store.get_file(table_id, fid)
                if not cur["is_live"] or cur["version"] != live[fid]["version"]:
                    raise StateConflictError("重写期间文件版本发生变化", file_id=fid)
            pf.write_parquet(arrow_table, self._parquet_path(table_id, new_file_id, 1))
            self.store.insert_file(
                conn, table_id=table_id, file_id=new_file_id, version=1,
                row_count=len(merged_rows), fingerprint=fingerprint,
                created_seq=table["next_seq"] - 1, ts=utc_now_iso(),
            )
            self.store.supersede_files(conn, table_id, old_keys, new_file_id)
        self._lineage(table_id).write(new_file_id, 1, merged_seqs,
                           {"kind": "rewrite", "parents": parents})

        after = self.evaluate_current(table_id)
        return {
            "new_file_id": new_file_id, "version": 1,
            "rows_written": len(merged_rows),
            "superseded": [list(k) for k in old_keys],
            "fingerprint": fingerprint,
            "report": self._report_summary(after),
        }

    # ================= 查询 =================
    def query(
        self, table_id: str,
        filters: list[dict[str, Any]] | None = None,
        columns: list[str] | None = None,
        trace: Callable[[dict], None] | None = None,
    ) -> dict[str, Any]:
        table = self.store.get_table(table_id)
        if columns is not None:
            bad = [c for c in columns if c not in table["columns"]]
            if bad:
                raise InputError("投影列不存在", unknown=bad)
        predicates = [_compile_filter(f, table["columns"]) for f in (filters or [])]

        report = self.evaluate_current(table_id, trace=trace)
        rows: list[dict[str, Any]] = []
        for v in report.kept_rows():
            if all(p(v.values) for p in predicates):
                vals = v.values if columns is None else {c: v.values.get(c) for c in columns}
                rows.append({
                    "file_id": v.file_id, "row_number": v.row_number,
                    "insert_seq": v.insert_seq, "keep_reason": v.reason, "values": vals,
                })
        return {
            "table_id": table_id,
            "seq_horizon": report.seq_horizon,
            "files": report.files,
            "deleted_count": len(report.deleted_rows()),
            "rows": rows,
        }

    def explain_row(self, table_id: str, file_id: str, row_number: int) -> dict[str, Any]:
        """给出某物理行的判定依据（调试/复核接口）。"""
        report = self.evaluate_current(table_id)
        for v in report.verdicts:
            if v.file_id == file_id and v.row_number == row_number:
                return {
                    "file_id": file_id, "row_number": row_number,
                    "action": v.action, "reason": v.reason,
                    "by_delete_id": v.by_delete_id, "by_seq": v.by_seq,
                    "insert_seq": v.insert_seq, "values": v.values,
                }
        raise NotFoundError("该行号在当前文件中不存在", file_id=file_id, row_number=row_number)

    def snapshot(self, table_id: str) -> dict[str, Any]:
        """完整快照：逐行结论 + 操作评估 + 状态。用于参考对照与重放。"""
        trace_events: list[dict[str, Any]] = []
        report = self.evaluate_current(table_id, trace=trace_events.append)
        return {
            "table": self.describe_table(table_id),
            "files": self.store.list_files(table_id),
            "deletes": self.store.list_deletes(table_id),
            "verdicts": [_verdict_dict(v) for v in report.verdicts],
            "op_evaluations": [
                {"delete_id": e.delete_id, "kind": e.kind, "seq": e.seq,
                 "status": e.status,
                 "matched_rows": [list(x) for x in e.matched_rows]}
                for e in report.op_evaluations
            ],
            "kernel_trace": trace_events,
        }

    # ================= 杂项 =================
    def _parquet_path(self, table_id: str, file_id: str, version: int) -> Path:
        return self.tables_dir / table_id / f"{file_id}.v{version}.parquet"

    @staticmethod
    def _report_summary(report: km.ScanReport) -> dict[str, Any]:
        return {
            "seq_horizon": report.seq_horizon,
            "files": report.files,
            "total": len(report.verdicts),
            "kept": len(report.kept_rows()),
            "deleted": len(report.deleted_rows()),
            "verdicts": [_verdict_dict(v) for v in report.verdicts],
            "op_evaluations": [
                {"delete_id": e.delete_id, "kind": e.kind, "seq": e.seq,
                 "status": e.status, "matched_rows": [list(x) for x in e.matched_rows]}
                for e in report.op_evaluations
            ],
        }

    def state_summary(self, table_id: str) -> dict[str, Any]:
        """操作后状态摘要，写入 run 日志。"""
        try:
            t = self.describe_table(table_id)
            return {
                "table_id": table_id,
                "seq_horizon": t["seq_horizon"],
                "live_files": {f["file_id"]: f["version"]
                               for f in self.store.list_files(table_id, live_only=True)},
                "deletes": [{"delete_id": d["delete_id"], "kind": d["kind"], "seq": d["seq"]}
                            for d in self.store.list_deletes(table_id)],
            }
        except NotFoundError:
            return {"table_id": table_id, "exists": False}


def _verdict_dict(v: km.RowVerdict) -> dict[str, Any]:
    return {
        "file_id": v.file_id, "row_number": v.row_number,
        "insert_seq": v.insert_seq, "action": v.action, "reason": v.reason,
        "by_delete_id": v.by_delete_id, "by_seq": v.by_seq, "values": v.values,
    }


def _spec_payload(kind: str, req: dict[str, Any]) -> dict[str, Any]:
    """删除规格（幂等比较用，不含服务端分配的 seq）。"""
    if kind == km.POSITION:
        return {"kind": km.POSITION, "file_id": req.get("file_id"),
                "row_number": req.get("row_number")}
    return {"kind": km.EQUALITY, "key": req.get("key")}


def _check_predicate_types(vals: list[Any], key_cols: list[str], columns: dict[str, str]) -> None:
    """谓词值类型做静态校验；NULL 始终合法（语义上不命中任何行）。"""
    for col, val in zip(key_cols, vals):
        if val is None:
            continue
        t = columns[col]
        if t in ("int64", "int32") and (isinstance(val, bool) or not isinstance(val, int)):
            raise InputError(f"主键 {col!r} 的谓词需要整数", got=val)
        if t == "float64" and (isinstance(val, bool) or not isinstance(val, (int, float))):
            raise InputError(f"主键 {col!r} 的谓词需要数值", got=val)
        if t == "bool" and not isinstance(val, bool):
            raise InputError(f"主键 {col!r} 的谓词需要布尔值", got=val)
        if t == "string" and not isinstance(val, str):
            raise InputError(f"主键 {col!r} 的谓词需要字符串", got=val)


def _compile_filter(f: dict[str, Any], columns: dict[str, str]) -> Callable[[dict[str, Any]], bool]:
    col = f.get("column")
    op = f.get("op")
    val = f.get("value")
    if col not in columns:
        raise InputError("过滤列不存在", column=col)
    if op not in FILTER_OPS:
        raise InputError("不支持的过滤算子", op=op, allowed=sorted(FILTER_OPS))

    def pred(row: dict[str, Any]) -> bool:
        cell = row.get(col)
        if op == "is_null":
            return cell is None
        if op == "not_null":
            return cell is not None
        if cell is None:
            return False  # NULL 不参与 True 侧比较
        if op == "eq":
            return _safe_eq(cell, val)
        if op == "neq":
            return not _safe_eq(cell, val)
        for cmp_op in ("gt", "gte", "lt", "lte"):
            if op == cmp_op:
                if isinstance(cell, bool) or not isinstance(cell, (int, float)) or \
                   isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise InputError(f"过滤 {col} {op} 需要两侧均为数值")
                return {"gt": cell > val, "gte": cell >= val,
                        "lt": cell < val, "lte": cell <= val}[cmp_op]
        raise InputError("不可达")  # pragma: no cover

    return pred


def _safe_eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b
