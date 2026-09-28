"""编排层：把格式适配、执行内核、元数据事务组合成完整操作。

关键流程：
- stage_files：文件先完整写入暂存区并校验，失败时逐个文件登记清理记录并隔离。
- commit：幂等检查 -> 物理校验 -> BEGIN IMMEDIATE 串行事务 ->
  内核裁决 -> 冲突则记 REJECTED；接受则先把文件发布到数据区，再挂入新快照。
- sweep：扫描孤立暂存目录/孤立数据文件（带宽限期），逐文件登记并隔离。
"""

from __future__ import annotations

import errno
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lake_txn import errors, format_adapter, kernel
from lake_txn.config import Settings
from lake_txn.diagnostics import DiagnosticLogger
from lake_txn.format_adapter import ColumnSpec
from lake_txn.metadata import Catalog

# 暂存台账状态
STAGE_READY = "ready"
STAGE_CONSUMED = "consumed"
STAGE_FAILED = "failed"

# 提交日志状态
LOG_ACCEPTED = "ACCEPTED"
LOG_REJECTED = "REJECTED"
LOG_PUBLISH_FAILED = "PUBLISH_FAILED"

# 清理类别
CLEAN_STAGE_FAILED = "stage_failed"
CLEAN_STAGE_PUBLISHED = "stage_published"
CLEAN_ORPHAN_DATA = "orphan_data"
CLEAN_ORPHAN_STAGING = "orphan_staging"


@dataclass(frozen=True)
class StageFileInput:
    logical_name: str
    mode: str  # "inline"（服务写 Parquet）或 "import"（拷贝白名单目录中的本地文件）
    records: list[dict[str, Any]] | None = None
    source_path: str | None = None
    declared_partition: str | None = None  # import 模式需要声明分区
    declared_sha256: str | None = None     # import 模式可选校验


@dataclass(frozen=True)
class StagedFileInfo:
    logical_name: str
    path: str
    sha256: str
    size_bytes: int
    row_count: int
    partition: str


class LakeService:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog | None = None,
        log: DiagnosticLogger | None = None,
    ) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.catalog = catalog or Catalog(settings.db_path)
        self.log = log or DiagnosticLogger(redact_fields=settings.redact_fields)

    # ---------- 建表 ----------
    def create_table(
        self, name: str, columns: list[dict[str, str]], partition_column: str
    ) -> dict[str, Any]:
        if not name or "/" in name or name in {".", ".."}:
            raise errors.bad_request(errors.VALIDATION_ERROR, "非法表名", {"table": name})
        if not columns:
            raise errors.bad_request(errors.VALIDATION_ERROR, "列定义为空")
        names = [c.get("name") for c in columns]
        if any(not n for n in names) or len(set(names)) != len(names):
            raise errors.bad_request(errors.VALIDATION_ERROR, "列名缺失或重复", {"columns": names})
        if partition_column not in names:
            raise errors.bad_request(
                errors.VALIDATION_ERROR,
                "分区列必须是表列之一",
                {"partition_column": partition_column, "columns": names},
            )
        specs = [ColumnSpec(c["name"], c["type"]) for c in columns]
        format_adapter.build_schema(specs)  # 提前拒绝不支持的类型
        if self.catalog.get_table(name) is not None:
            raise errors.bad_request(errors.VALIDATION_ERROR, f"表已存在: {name}")
        self.catalog.create_table(name, columns, partition_column, time.time())
        self.settings.data_dir(name).mkdir(parents=True, exist_ok=True)
        self.log.info("table_created", table=name, columns=names, partition_column=partition_column)
        return {"table": name, "columns": columns, "partition_column": partition_column}

    # ---------- 暂存 ----------
    def stage_files(self, table: str, request_id: str, files: list[StageFileInput]):
        tdef = self._require_table(table)
        if not files:
            raise errors.bad_request(errors.VALIDATION_ERROR, "files 为空")
        logical = [f.logical_name for f in files]
        if len(set(logical)) != len(logical):
            raise errors.bad_request(errors.VALIDATION_ERROR, "logical_name 重复", {"names": logical})

        specs = [ColumnSpec(c["name"], c["type"]) for c in tdef.columns]
        stage_dir = self.settings.request_staging_dir(request_id)
        stage_dir.mkdir(parents=True, exist_ok=True)

        ready: list[StagedFileInfo] = []
        # 本次已落盘的 (物理路径, 逻辑名)；失败文件的逻辑名为 None
        produced_files: list[tuple[Path, str | None]] = []
        failures: list[dict[str, Any]] = []

        for f in files:
            produced: Path | None = None
            try:
                info, produced = self._stage_one(
                    table, f, specs, tdef.partition_column, stage_dir
                )
            except errors.DomainError as exc:
                produced_str = exc.detail.pop("_produced_path", None)
                failures.append(
                    {
                        "logical_name": f.logical_name,
                        "reason_code": exc.reason_code,
                        "message": exc.message,
                        "detail": exc.detail,
                    }
                )
                # 失败若已落盘（如写出后读回校验失败），该文件也必须被独立清理
                if produced_str and Path(produced_str).exists():
                    produced_files.append((Path(produced_str), None))
                continue
            ready.append(info)
            produced_files.append((produced, info.logical_name))

        if failures:
            # 全有或全无：本次已写出的文件逐个隔离并独立登记
            for physical, logical_name in produced_files:
                self._quarantine_file(
                    physical,
                    CLEAN_STAGE_FAILED,
                    errors.STAGE_VALIDATION_FAILED,
                    request_id=request_id,
                    table_name=table,
                    detail={"logical_name": logical_name or "(failed-file)",
                            "note": "同一暂存请求中存在失败文件"},
                )
            self.log.warning(
                "stage_rejected",
                table=table,
                request_id=request_id,
                failed=failures,
                accepted_count=0,
            )
            self._remove_stage_dir_if_empty(request_id)
            raise errors.unprocessable(
                errors.STAGE_VALIDATION_FAILED,
                f"{len(failures)} 个文件暂存失败，整个暂存请求被拒绝",
                {"failures": failures, "quarantined": len(produced_files)},
            )

        entries = [
            {
                "logical_name": i.logical_name,
                "path": i.path,
                "sha256": i.sha256,
                "size_bytes": i.size_bytes,
                "row_count": i.row_count,
                "partition": i.partition,
            }
            for i in ready
        ]
        self.catalog.upsert_staged_ready(request_id, table, entries)
        self.log.info(
            "stage_accepted",
            table=table,
            request_id=request_id,
            files=[
                {
                    "logical_name": i.logical_name,
                    "partition": i.partition,
                    "row_count": i.row_count,
                    "sha256": i.sha256[:12],
                }
                for i in ready
            ],
            file_count=len(ready),
        )
        return {
            "table": table,
            "request_id": request_id,
            "status": "ready",
            "files": [e | {"path": _short(e["path"])} for e in entries],
        }

    def _stage_one(
        self,
        table: str,
        f: StageFileInput,
        specs: list[ColumnSpec],
        partition_column: str,
        stage_dir: Path,
    ) -> tuple[StagedFileInfo, Path]:
        if f.mode == "inline":
            if not f.records:
                raise errors.bad_request(
                    errors.VALIDATION_ERROR, "inline 文件缺少 records", {"logical_name": f.logical_name}
                )
            partition = self._single_partition(f.records, partition_column, f.logical_name)
            tmp = format_adapter.write_parquet_atomic(
                f.records, specs, stage_dir, f"{f.logical_name}.parquet"
            )
            physical = tmp.path
            digest, rows = tmp.sha256, tmp.row_count
            # 写回时即读回校验，保证“完整写入再可用”；若校验失败把已落盘路径带出
            try:
                format_adapter.verify_parquet(physical, specs, digest, rows)
            except errors.DomainError as exc:
                exc.detail["_produced_path"] = str(physical)
                raise
            self.log.info(
                "stage_file_written",
                logical_name=f.logical_name,
                partition=partition,
                row_count=rows,
                sample=f.records[0],
            )
        elif f.mode == "import":
            physical = self._import_file(f, specs, stage_dir)
            digest, rows = format_adapter.verify_parquet(
                physical, specs, f.declared_sha256 or format_adapter.sha256_file(physical)
            )
            if f.declared_sha256 is not None and digest != f.declared_sha256:
                raise errors.unprocessable(
                    errors.FILE_HASH_MISMATCH,
                    "import 文件指纹与声明不一致",
                    {"logical_name": f.logical_name, "_produced_path": str(physical)},
                )
            if not f.declared_partition:
                raise errors.bad_request(
                    errors.VALIDATION_ERROR,
                    "import 文件必须声明 declared_partition",
                    {"logical_name": f.logical_name, "_produced_path": str(physical)},
                )
            partition = f.declared_partition
        else:
            raise errors.bad_request(
                errors.VALIDATION_ERROR,
                "mode 必须是 inline 或 import",
                {"logical_name": f.logical_name, "mode": f.mode},
            )
        rel = physical.relative_to(self.settings.root).as_posix()
        return (
            StagedFileInfo(
                logical_name=f.logical_name,
                path=rel,
                sha256=digest,
                size_bytes=physical.stat().st_size,
                row_count=rows,
                partition=partition,
            ),
            physical,
        )

    def _import_file(self, f: StageFileInput, specs: list[ColumnSpec], stage_dir: Path) -> Path:
        if not f.source_path:
            raise errors.bad_request(
                errors.VALIDATION_ERROR, "import 文件缺少 source_path", {"logical_name": f.logical_name}
            )
        src = Path(f.source_path).resolve()
        allowed = any(_is_within(src, base) for base in self.settings.allowed_inbound_dirs)
        if not allowed:
            raise errors.bad_request(
                errors.IMPORT_PATH_FORBIDDEN,
                "source_path 不在允许的本地白名单目录内",
                {"logical_name": f.logical_name},
            )
        if not src.is_file():
            raise errors.bad_request(
                errors.FILE_NOT_STAGED,
                "import 源文件不存在",
                {"logical_name": f.logical_name},
            )
        dest = stage_dir / f"{f.logical_name}.parquet"
        try:
            os.link(src, dest)  # 同一文件系统优先硬链接（不可变数据，安全）
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES):
                raise
            with src.open("rb") as fin, dest.open("wb") as fout:
                shutil.copyfileobj(fin, fout, length=1024 * 1024)
                fout.flush()
                os.fsync(fout.fileno())
        return dest

    @staticmethod
    def _single_partition(
        records: list[dict[str, Any]], partition_column: str, logical_name: str
    ) -> str:
        values = {r.get(partition_column) for r in records}
        if len(values) != 1 or next(iter(values)) is None:
            raise errors.bad_request(
                errors.VALIDATION_ERROR,
                "每个文件的记录必须属于同一个非空分区",
                {"logical_name": logical_name, "partition_column": partition_column, "values": sorted(map(str, values))},
            )
        return str(next(iter(values)))

    # ---------- 提交 ----------
    def commit(
        self,
        table: str,
        request_id: str,
        kind: str,
        base_snapshot_id: int,
        logical_names: list[str],
        drop_partitions: list[str] | None = None,
    ) -> dict[str, Any]:
        tdef = self._require_table(table)
        kind_enum = kernel.CommitKind(kind)
        drop = frozenset(drop_partitions or [])
        if kind_enum is kernel.CommitKind.OVERWRITE and not drop:
            raise errors.bad_request(
                errors.VALIDATION_ERROR, "OVERWRITE 必须声明非空 drop_partitions"
            )
        if kind_enum is kernel.CommitKind.APPEND and drop:
            raise errors.bad_request(
                errors.VALIDATION_ERROR, "APPEND 不允许 drop_partitions"
            )

        # 1) 幂等：同一 request_id 的重放必须返回首次结果；改了请求体则拒绝
        prior = self.catalog.get_commit_for_table(request_id, table)
        if prior is not None:
            same_scope = (
                prior.commit_kind == kind
                and prior.base_snapshot_id == base_snapshot_id
                and frozenset(prior.detail.get("logical_names", ())) == frozenset(logical_names)
                and frozenset(prior.detail.get("drop_partitions", ())) == drop
            )
            if not same_scope:
                raise errors.rejected(
                    errors.REQUEST_SCOPE_MISMATCH,
                    f"request_id={request_id} 已用于不同范围的提交",
                    {"prior_status": prior.status, "prior_snapshot_id": prior.snapshot_id},
                )
            self.log.info(
                "commit_replayed",
                table=table,
                request_id=request_id,
                prior_status=prior.status,
                prior_snapshot_id=prior.snapshot_id,
                idempotent=True,
            )
            return self._commit_result(prior)

        # 2) 暂存解析与物理校验
        staged = self._resolve_staged(table, request_id, logical_names)
        specs = [ColumnSpec(c["name"], c["type"]) for c in tdef.columns]
        for row in staged:
            physical = self.settings.root / row["path"]
            format_adapter.verify_parquet(
                physical, specs, row["sha256"], row["row_count"]
            )
        add_partitions = frozenset(r["partition"] for r in staged)
        if kind_enum is kernel.CommitKind.OVERWRITE and not add_partitions <= drop:
            raise errors.bad_request(
                errors.VALIDATION_ERROR,
                "OVERWRITE 新文件的分区必须包含在 drop_partitions 中",
                {"add_partitions": sorted(add_partitions), "drop_partitions": sorted(drop)},
            )

        intent = kernel.CommitIntent(
            table=table,
            request_id=request_id,
            kind=kind_enum,
            base_snapshot_id=base_snapshot_id,
            add_partitions=add_partitions,
            drop_partitions=drop,
        )

        # 3) 串行事务 + 内核裁决
        with self.catalog.lock:
            conn = self.catalog  # 便捷别名
            db = conn._conn
            db.execute("BEGIN IMMEDIATE")
            cur = db.cursor()
            try:
                head = conn.head_snapshot_id(table)
                base_row = conn.get_snapshot(table, base_snapshot_id)
                if base_row is None:
                    db.execute("ROLLBACK")
                    raise errors.bad_request(
                        errors.UNKNOWN_BASE,
                        f"基线快照不存在: {base_snapshot_id}",
                        {"base_snapshot_id": base_snapshot_id, "head_snapshot_id": head},
                    )
                if base_snapshot_id > head:
                    conn.insert_commit(
                        cur, request_id, table, kind, base_snapshot_id,
                        LOG_REJECTED, errors.BASE_IN_FUTURE, None,
                        {"base_snapshot_id": base_snapshot_id, "head_snapshot_id": head,
                         "logical_names": logical_names, "drop_partitions": sorted(drop)},
                        time.time(),
                    )
                    db.execute("COMMIT")
                    raise errors.rejected(
                        errors.BASE_IN_FUTURE,
                        "基线快照新于当前表头",
                        {"base_snapshot_id": base_snapshot_id, "head_snapshot_id": head},
                    )

                concurrent_rows = conn.commits_between(table, base_snapshot_id, head)
                concurrent = tuple(
                    kernel.CompetingCommit(
                        request_id=r.request_id,
                        kind=kernel.CommitKind(r.commit_kind),
                        partitions=frozenset(r.detail.get("add_partitions", []))
                        | frozenset(r.detail.get("drop_partitions", [])),
                    )
                    for r in concurrent_rows
                )
                decision = kernel.adjudicate(intent, head, concurrent)

                if not decision.accepted:
                    detail = {
                        "base_snapshot_id": base_snapshot_id,
                        "head_snapshot_id": head,
                        "concurrent_request_ids": list(decision.concurrent_request_ids),
                        "conflict_partitions": sorted(decision.conflict_partitions),
                        "add_partitions": sorted(add_partitions),
                        "drop_partitions": sorted(drop),
                        "logical_names": logical_names,
                    }
                    conn.insert_commit(
                        cur, request_id, table, kind, base_snapshot_id,
                        LOG_REJECTED, decision.reason_code, None, detail, time.time(),
                    )
                    db.execute("COMMIT")
                    self.log.warning(
                        "commit_rejected",
                        table=table,
                        request_id=request_id,
                        reason_code=decision.reason_code,
                        base_snapshot_id=base_snapshot_id,
                        head_snapshot_id=head,
                        concurrent_request_ids=list(decision.concurrent_request_ids),
                        conflict_partitions=sorted(decision.conflict_partitions),
                    )
                    raise errors.rejected(
                        decision.reason_code,
                        _reason_message(decision.reason_code),
                        detail,
                    )

                # 4) 接受：先发布物理文件（不可变数据区），再挂入快照
                parent_id = head if decision.merged else base_snapshot_id
                parent_files = tuple(
                    kernel.ManifestFile(
                        path=m["path"],
                        partition=m["partition"],
                        sha256=m["sha256"],
                        size_bytes=m["size_bytes"],
                        row_count=m["row_count"],
                    )
                    for m in conn.manifest_rows(table, parent_id)
                )
                new_files = tuple(
                    kernel.ManifestFile(
                        path=self._target_relpath(table, r),
                        partition=r["partition"],
                        sha256=r["sha256"],
                        size_bytes=r["size_bytes"],
                        row_count=r["row_count"],
                    )
                    for r in staged
                )
                planned = kernel.plan_manifest(
                    kind_enum, parent_files, new_files, drop
                )
                planned_dicts = [
                    {
                        "path": f.path,
                        "partition": f.partition,
                        "sha256": f.sha256,
                        "size_bytes": f.size_bytes,
                        "row_count": f.row_count,
                    }
                    for f in planned
                ]
                removed = sum(1 for f in parent_files if f.path not in {p.path for p in planned})

                published: list[tuple[Path, Path]] = []  # (staged, target)
                try:
                    for r, target in zip(staged, [self.settings.root / f.path for f in new_files]):
                        src = self.settings.root / r["path"]
                        try:
                            self._publish_one(src, target)
                        except errors.DomainError:
                            raise
                        except OSError as exc:
                            # 意外文件系统错误也归为可分类的发布失败，而不是 500
                            target.unlink(missing_ok=True)
                            raise errors.unprocessable(
                                errors.PUBLISH_FAILURE,
                                f"发布文件时发生文件系统错误: {exc}",
                                {"src": src.name, "dest": target.name},
                            ) from exc
                        published.append((src, target))
                except errors.DomainError:
                    db.execute("ROLLBACK")
                    self._handle_publish_failure(table, request_id, kind, base_snapshot_id,
                                                 logical_names, drop, staged, published)
                    raise

                snapshot_id = conn.insert_snapshot(
                    cur, table, parent_id, kind, request_id, time.time(),
                    added=len(new_files), removed=removed, total=len(planned), files=planned_dicts,
                )
                conn.insert_commit(
                    cur, request_id, table, kind, base_snapshot_id,
                    LOG_ACCEPTED, None, snapshot_id,
                    {"base_snapshot_id": base_snapshot_id, "head_snapshot_id_before": head,
                     "parent_snapshot_id": parent_id, "merged": decision.merged,
                     "concurrent_request_ids": list(decision.concurrent_request_ids),
                     "add_partitions": sorted(add_partitions),
                     "drop_partitions": sorted(drop),
                     "logical_names": logical_names},
                    time.time(),
                )
                conn.mark_staged_status(
                    cur, request_id, logical_names, STAGE_CONSUMED
                )
                db.execute("COMMIT")
            except errors.DomainError:
                raise
            except BaseException:
                db.execute("ROLLBACK")
                raise

        # 5) 事务提交后清理暂存物理文件（逐文件独立登记）
        for r in staged:
            stale = self.settings.root / r["path"]
            if stale.exists():
                stale.unlink()
                self.catalog.add_cleanup(
                    CLEAN_STAGE_PUBLISHED, str(stale.relative_to(self.settings.root)),
                    "PUBLISHED", "deleted", time.time(), request_id=request_id, table_name=table,
                    detail={"logical_name": r["logical_name"]},
                )
        self._remove_stage_dir_if_empty(request_id)

        result = {
            "status": LOG_ACCEPTED,
            "table": table,
            "request_id": request_id,
            "snapshot_id": snapshot_id,
            "parent_snapshot_id": parent_id,
            "base_snapshot_id": base_snapshot_id,
            "merged": decision.merged,
            "added_files": len(new_files),
            "removed_files": removed,
            "total_files": len(planned),
            "concurrent_request_ids": list(decision.concurrent_request_ids),
        }
        self.log.info("commit_accepted", **result)
        return result

    def _resolve_staged(self, table, request_id, logical_names):
        if not logical_names:
            raise errors.bad_request(errors.VALIDATION_ERROR, "提交未引用任何暂存文件")
        rows = self.catalog.get_staged_ready(request_id)
        by_name = {r["logical_name"]: r for r in rows}
        missing = [n for n in logical_names if n not in by_name]
        if missing:
            raise errors.bad_request(
                errors.FILE_NOT_STAGED,
                "存在未就绪/未暂存的文件引用",
                {"request_id": request_id, "missing": missing},
            )
        extra = [r["logical_name"] for r in rows if r["logical_name"] not in logical_names]
        if extra:
            raise errors.bad_request(
                errors.VALIDATION_ERROR,
                "请求中仍有未提交的就绪暂存文件",
                {"request_id": request_id, "extra": extra},
            )
        bad_table = {r["logical_name"] for r in rows if r["table_name"] != table}
        if bad_table:
            raise errors.bad_request(
                errors.FILE_PARTITION_MISMATCH,
                "暂存文件属于其它表",
                {"logical_names": sorted(bad_table), "expected_table": table},
            )
        return [dict(by_name[n]) for n in logical_names]

    def _target_relpath(self, table: str, row) -> str:
        target = self.settings.data_dir(table) / row["partition"] / f"{row['sha256']}.parquet"
        return target.relative_to(self.settings.root).as_posix()

    @staticmethod
    def _publish_one(src: Path, dest: Path) -> None:
        """把完整暂存文件挂入数据区。目标内容寻址、不可变；已存在即视为同一文件。"""
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dest)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES):
                raise errors.unprocessable(
                    errors.PUBLISH_FAILURE,
                    f"发布文件失败: {exc}",
                    {"src": src.name, "dest": dest.name},
                )
            try:
                with src.open("rb") as fin, dest.open("wb") as fout:
                    shutil.copyfileobj(fin, fout, length=1024 * 1024)
                    fout.flush()
                    os.fsync(fout.fileno())
            except OSError as exc2:
                dest.unlink(missing_ok=True)
                raise errors.unprocessable(
                    errors.PUBLISH_FAILURE,
                    f"发布文件失败: {exc2}",
                    {"src": src.name, "dest": dest.name},
                ) from exc2

    def _handle_publish_failure(
        self, table, request_id, kind, base, logical_names, drop, staged, published
    ) -> None:
        """发布失败：不产生快照；逐文件隔离已发布/已暂存文件，登记 PUBLISH_FAILED。"""
        now = time.time()
        for src, target in published:
            self._quarantine_file(
                target, CLEAN_STAGE_FAILED, errors.PUBLISH_FAILURE,
                request_id=request_id, table_name=table,
                detail={"phase": "publish", "note": "已发布但未挂入任何快照"},
            )
        published_src = {s for s, _ in published}
        for r in staged:
            src = self.settings.root / r["path"]
            if src in published_src:
                continue
            if src.exists():
                self._quarantine_file(
                    src, CLEAN_STAGE_FAILED, errors.PUBLISH_FAILURE,
                    request_id=request_id, table_name=table,
                    detail={"logical_name": r["logical_name"], "phase": "publish"},
                )
        with self.catalog.lock:
            db = self.catalog._conn
            db.execute("BEGIN IMMEDIATE")
            cur = db.cursor()
            try:
                self.catalog.mark_staged_status(
                    cur, request_id, logical_names, STAGE_FAILED
                )
                self.catalog.insert_commit(
                    cur, request_id, table, kind, base,
                    LOG_PUBLISH_FAILED, errors.PUBLISH_FAILURE, None,
                    {"logical_names": logical_names, "drop_partitions": sorted(drop),
                     "note": "物理发布失败，文件已逐个隔离；请重新暂存后以新 request_id 提交"},
                    now,
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        self.log.error(
            "publish_failed",
            table=table,
            request_id=request_id,
            reason_code=errors.PUBLISH_FAILURE,
            published_files=len(published),
            total_files=len(staged),
        )

    # ---------- 读取 ----------
    def get_snapshot_detail(self, table: str, snapshot_id: int) -> dict[str, Any]:
        self._require_table(table)
        snap = self.catalog.get_snapshot(table, snapshot_id)
        if snap is None:
            raise errors.bad_request(
                errors.UNKNOWN_BASE, f"快照不存在: {snapshot_id}",
                {"snapshot_id": snapshot_id},
            )
        return {
            "snapshot_id": snap.id,
            "parent_snapshot_id": snap.parent_id,
            "commit_kind": snap.commit_kind,
            "request_id": snap.request_id,
            "created_at": snap.created_at,
            "added_files": snap.added_files,
            "removed_files": snap.removed_files,
            "total_files": snap.total_files,
            "files": [
                {
                    "path": _short(m["path"]),
                    "partition": m["partition"],
                    "sha256": m["sha256"],
                    "size_bytes": m["size_bytes"],
                    "row_count": m["row_count"],
                }
                for m in self.catalog.manifest_rows(table, snap.id)
            ],
        }

    def read_snapshot_rows(self, table: str, snapshot_id: int) -> list[dict[str, Any]]:
        """直接读取快照行集（物理扫描，供校验与工具使用）。"""
        rows: list[dict[str, Any]] = []
        for m in self.catalog.manifest_rows(table, snapshot_id):
            rows.extend(format_adapter.read_rows(self.settings.root / m["path"]))
        return rows

    def list_tables(self) -> dict[str, Any]:
        out = []
        for name in self.catalog.list_tables():
            tdef = self.catalog.get_table(name)
            out.append(
                {
                    "table": name,
                    "head_snapshot_id": self.catalog.head_snapshot_id(name),
                    "partition_column": tdef.partition_column,
                    "columns": list(tdef.columns),
                }
            )
        return {"tables": out}

    def list_snapshots(self, table: str) -> dict[str, Any]:
        self._require_table(table)
        return {
            "table": table,
            "head_snapshot_id": self.catalog.head_snapshot_id(table),
            "snapshots": [
                {
                    "snapshot_id": s.id,
                    "parent_snapshot_id": s.parent_id,
                    "commit_kind": s.commit_kind,
                    "request_id": s.request_id,
                    "added_files": s.added_files,
                    "removed_files": s.removed_files,
                    "total_files": s.total_files,
                }
                for s in self.catalog.list_snapshots(table)
            ],
        }

    def get_commit_status(self, request_id: str, table: str | None = None) -> dict[str, Any]:
        row = (
            self.catalog.get_commit_for_table(request_id, table)
            if table
            else self.catalog.get_commit(request_id)
        )
        if row is None:
            raise errors.bad_request(
                "UNKNOWN_REQUEST",
                f"没有 request_id={request_id} 的提交记录，状态无法判定",
                {"request_id": request_id},
            )
        return self._commit_result(row, include_reason=True)

    def _commit_result(self, row, include_reason: bool = True) -> dict[str, Any]:
        out = {
            "status": row.status,
            "table": row.table_name,
            "request_id": row.request_id,
            "snapshot_id": row.snapshot_id,
            "idempotent_replay": True,
        }
        if include_reason and row.reason_code:
            out["reason_code"] = row.reason_code
            out["detail"] = row.detail
        return out

    def list_cleanup(self, request_id: str | None = None, kind: str | None = None) -> dict[str, Any]:
        rows = self.catalog.list_cleanup(request_id=request_id, kind=kind)
        return {
            "records": [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "request_id": r["request_id"],
                    "table_name": r["table_name"],
                    "src_path": _short(r["src_path"]),
                    "dest_path": _short(r["dest_path"]) if r["dest_path"] else None,
                    "reason_code": r["reason_code"],
                    "status": r["status"],
                }
                for r in rows
            ]
        }

    # ---------- 孤立文件清扫 ----------
    def sweep(self, grace_seconds: int | None = None) -> dict[str, Any]:
        grace = self.settings.orphan_grace_seconds if grace_seconds is None else grace_seconds
        now = time.time()
        quarantined: list[dict[str, Any]] = []

        # a) 暂存区：没有任何 ready 台账记录、且超过宽限期的文件
        if self.settings.staging_dir.exists():
            for rid_dir in sorted(p for p in self.settings.staging_dir.iterdir() if p.is_dir()):
                rid = rid_dir.name
                ready_paths = {
                    str((self.settings.root / r["path"]).resolve())
                    for r in self.catalog.get_staged_ready(rid)
                }
                for f in sorted(rid_dir.rglob("*")):
                    if not f.is_file() or now - f.stat().st_mtime < grace:
                        continue
                    if str(f.resolve()) in ready_paths:
                        continue  # 在途就绪文件，绝不动
                    rec = self._quarantine_file(
                        f, CLEAN_ORPHAN_STAGING, "ORPHAN_STAGING_FILE",
                        request_id=rid, detail={"mtime_age_seconds": round(now - f.stat().st_mtime, 1)},
                    )
                    quarantined.append(rec)
                # 清掉空目录
                for d in sorted((p for p in rid_dir.rglob("*") if p.is_dir()), reverse=True):
                    try:
                        d.rmdir()
                    except OSError:
                        pass
                try:
                    rid_dir.rmdir()
                except OSError:
                    pass

        # b) 数据区：不在任何快照清单中的 parquet（发布后未挂入/历史遗留）
        for name in self.catalog.list_tables():
            referenced = {
                str((self.settings.root / p).resolve()) for p in self.catalog.all_data_paths(name)
            }
            data_dir = self.settings.data_dir(name)
            if not data_dir.exists():
                continue
            for f in sorted(data_dir.rglob("*.parquet")):
                if now - f.stat().st_mtime < grace:
                    continue
                if str(f.resolve()) in referenced:
                    continue
                rec = self._quarantine_file(
                    f, CLEAN_ORPHAN_DATA, "ORPHAN_DATA_FILE", table_name=name,
                    detail={"mtime_age_seconds": round(now - f.stat().st_mtime, 1)},
                )
                quarantined.append(rec)

        self.log.info("sweep_completed", quarantined_count=len(quarantined), grace_seconds=grace)
        return {
            "grace_seconds": grace,
            "quarantined_count": len(quarantined),
            "records": [
                {"kind": r["kind"], "src_path": _short(r["src"]), "reason_code": r["reason"]}
                for r in quarantined
            ],
        }

    # ---------- 公共辅助 ----------
    def _quarantine_file(
        self,
        path: Path,
        kind: str,
        reason: str,
        request_id: str | None = None,
        table_name: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把文件移入隔离区（绝不直接删用户数据），并逐文件写清理台账。"""
        path = Path(path)
        now = time.time()
        dest_rel: str | None = None
        status = "deleted"
        if path.exists():
            dest_dir = self.settings.quarantine_dir / kind
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{now_ns()}-{path.name}"
            shutil.move(str(path), str(dest))
            dest_rel = dest.relative_to(self.settings.root).as_posix()
            status = "quarantined"
        ledger_id = self.catalog.add_cleanup(
            kind,
            path.relative_to(self.settings.root).as_posix()
            if _is_within(path, self.settings.root)
            else str(path),
            reason,
            status,
            now,
            request_id=request_id,
            table_name=table_name,
            dest_path=dest_rel,
            detail=detail,
        )
        return {"id": ledger_id, "kind": kind, "src": str(path), "reason": reason}

    def _remove_stage_dir_if_empty(self, request_id: str) -> None:
        d = self.settings.request_staging_dir(request_id)
        if d.exists() and not any(d.iterdir()):
            d.rmdir()

    def _require_table(self, name: str):
        tdef = self.catalog.get_table(name)
        if tdef is None:
            raise errors.bad_request(errors.UNKNOWN_TABLE, f"表不存在: {name}", {"table": name})
        return tdef


# ---------------- 纯辅助 ----------------
def now_ns() -> str:
    return f"{time.time_ns()}"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _short(p: str | None) -> str | None:
    if p is None:
        return None
    # 日志/响应中使用相对仓库根的短路径已经足够；此处仅做保险裁剪
    return p


def _reason_message(code: str) -> str:
    return {
        errors.PARTITION_CONFLICT: "与并发提交存在重叠分区，冲突已判定，请刷新快照后重试",
        errors.CONCURRENT_OVERWRITE: "并发覆盖冲突：存在重叠分区的并发 OVERWRITE，不能自动合并",
        errors.STALE_BASE_OVERWRITE: "OVERWRITE 基线陈旧且存在并发提交，请刷新快照后重放覆盖",
        errors.BASE_IN_FUTURE: "基线快照新于当前表头",
    }.get(code, code)
