"""服务层：编排文本规范、算法索引、版本存储与诊断，对外暴露与传输无关的 API。

所有写操作都记录 operation_log（run_id、关键中间状态、错误类别），
失败与成功同样记录，使问题可以凭 run_id 重放。
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

import grapheme

from .config import Settings
from .diagnostics import LoadedVersion, describe_clusters, load_version
from .errors import (
    DocumentConflictError,
    DocumentNotFoundError,
    EmptyFieldError,
    IndexInconsistentError,
    InvalidBase64Error,
    LimitExceededError,
    VersionConflictError,
    VersionNotFoundError,
)
from .indexing import (
    apply_edit,
    build_index,
    plan_edit,
)
from .logging_setup import log_event
from .storage import VersionStore
from .textnorm import compute_stats, decode_strict, sha256_hex


@dataclass
class CreateResult:
    doc_id: str
    version: int
    stats: dict[str, Any]
    content_sha256: str
    gcb_table_version: str
    unidata_version: str


class TextIndexService:
    def __init__(self, store: VersionStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    # ── 解码与限制 ──────────────────────────────────────────────────────────
    def _decode_payload(self, content_base64: str) -> bytes:
        if not isinstance(content_base64, str) or content_base64 == "":
            raise EmptyFieldError("content_base64 不能为空")
        try:
            # validate=True：拒绝非字母表字符/填充错误
            raw = base64.b64decode(content_base64.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
            raise InvalidBase64Error(
                "内容不是合法的标准 base64", details={"reason": str(exc)}
            ) from exc
        decode_strict(raw)  # 非法 UTF-8 拒绝（自实现严格判定，带定位）
        return raw

    def _enforce_content_limits(self, raw: bytes, text: str) -> None:
        s = self.settings
        if len(raw) > s.max_bytes:
            raise LimitExceededError(
                "文档字节数超过上限",
                details={"actual": len(raw), "limit": s.max_bytes, "resource": "bytes"},
            )
        if len(text) > s.max_codepoints:
            raise LimitExceededError(
                "码点数超过上限",
                details={"actual": len(text), "limit": s.max_codepoints,
                         "resource": "codepoints"},
            )
        cluster_count = grapheme.length(text)
        if cluster_count > s.max_clusters:
            raise LimitExceededError(
                "扩展字素簇数超过上限",
                details={"actual": cluster_count, "limit": s.max_clusters,
                         "resource": "clusters"},
            )

    # ── 加载（带三重完整性校验）──────────────────────────────────────────────
    def _load(self, doc_id: str, version: int, raw: bytes) -> LoadedVersion:
        return load_version(
            self.store,
            doc_id,
            version,
            raw_content=raw,
            expected_gcb=self.settings.gcb_table_version,
            expected_unidata=self.settings.unidata_version,
        )

    def _require_document(self, doc_id: str) -> None:
        if not self.store.document_exists(doc_id):
            raise DocumentNotFoundError(
                "文档不存在", details={"doc_id": doc_id}
            )

    def _get_raw(self, doc_id: str, version: int) -> bytes:
        raw = self.store.get_content(doc_id, version)
        if raw is None:
            raise VersionNotFoundError(
                "版本不存在", details={"doc_id": doc_id, "version": version}
            )
        return raw

    # ── 创建 ────────────────────────────────────────────────────────────────
    def create_document(
        self, doc_id: str, content_base64: str, *, run_id: str
    ) -> CreateResult:
        detail: dict[str, Any] = {"doc_id": doc_id, "phase": "decode"}
        try:
            if self.store.document_exists(doc_id):
                raise DocumentConflictError(
                    "doc_id 已存在", details={"doc_id": doc_id}
                )
            if self.store.count_documents() >= self.settings.max_documents:
                raise LimitExceededError(
                    "文档总数超过上限",
                    details={"limit": self.settings.max_documents,
                             "resource": "documents"},
                )
            raw = self._decode_payload(content_base64)
            text = raw.decode("utf-8")
            self._enforce_content_limits(raw, text)
            stats = compute_stats(text, raw)
            detail.update(
                phase="build",
                intermediate={"byte_count": len(raw), "codepoint_count": len(text)},
            )
            index = build_index(text)
            digest = sha256_hex(raw)

            self.store.create_document_with_version(
                doc_id=doc_id, index=index, raw=raw,
                content_sha256=digest,
                gcb_table_version=self.settings.gcb_table_version,
                unidata_version=self.settings.unidata_version,
            )
            detail["phase"] = "done"
            detail["result"] = {"version": 0, "clusters": index.cluster_count}
            self.store.log_operation(
                run_id=run_id, op="create", doc_id=doc_id, version=0,
                success=True, detail=detail,
            )
            log_event("document_created", doc_id=doc_id, version=0,
                      clusters=index.cluster_count, bytes=len(raw))
            return CreateResult(
                doc_id=doc_id, version=0, stats=stats.to_dict(),
                content_sha256=digest,
                gcb_table_version=self.settings.gcb_table_version,
                unidata_version=self.settings.unidata_version,
            )
        except Exception as exc:
            self._log_failure("create", run_id, doc_id, None, detail, exc)
            raise

    # ── 编辑（增量更新 + 强制对照完整重建）───────────────────────────────────
    def edit_document(
        self,
        doc_id: str,
        *,
        expected_version: int,
        start: int,
        end: int,
        space: str,
        replacement_base64: str,
        run_id: str,
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "doc_id": doc_id, "expected_version": expected_version,
            "space": space, "start": start, "end": end, "phase": "load",
        }
        try:
            self._require_document(doc_id)
            doc = self.store.get_document(doc_id)
            assert doc is not None
            if expected_version != doc["current_version"]:
                raise VersionConflictError(
                    "expected_version 已过期",
                    details={
                        "expected": expected_version,
                        "current": doc["current_version"],
                    },
                )
            raw = self._get_raw(doc_id, expected_version)
            loaded = self._load(doc_id, expected_version, raw)

            detail["phase"] = "decode_replacement"
            repl_text = self._decode_b64_utf8(replacement_base64)
            edit = plan_edit(loaded.index, start, end, space, repl_text)
            detail["edit_codepoints"] = [edit.start_cp, edit.end_cp]
            detail["replacement_codepoints"] = len(repl_text)

            detail["phase"] = "incremental"
            incremental = apply_edit(loaded.index, edit)

            build_mode = "incremental_verified"
            if self.settings.verify_incremental:
                detail["phase"] = "full_rebuild_verify"
                rebuilt = build_index(incremental.text)
                if (
                    incremental.cp_to_byte != rebuilt.cp_to_byte
                    or incremental.cp_to_cluster != rebuilt.cp_to_cluster
                    or incremental.cluster_to_cp != rebuilt.cluster_to_cp
                ):
                    raise IndexInconsistentError(
                        "增量更新与完整重建结果不一致",
                        details={
                            "doc_id": doc_id,
                            "base_version": expected_version,
                            "incremental": {
                                "cluster_to_cp": list(incremental.cluster_to_cp),
                            },
                            "rebuilt": {
                                "cluster_to_cp": list(rebuilt.cluster_to_cp),
                            },
                        },
                    )
            else:
                build_mode = "incremental_unverified"

            new_raw = incremental.text.encode("utf-8")
            self._enforce_content_limits(new_raw, incremental.text)
            digest = sha256_hex(new_raw)
            new_version = expected_version + 1
            self.store.save_version_with_content(
                doc_id=doc_id, version=new_version, index=incremental, raw=new_raw,
                content_sha256=digest,
                gcb_table_version=self.settings.gcb_table_version,
                unidata_version=self.settings.unidata_version,
                build_mode=build_mode,
                parent_version=expected_version,
                edit_info={
                    "op": "edit",
                    "space": space,
                    "start": start,
                    "end": end,
                    "replacement_sha256": sha256_hex(repl_text.encode("utf-8")),
                },
            )
            detail["phase"] = "done"
            detail["result"] = {
                "version": new_version,
                "clusters": incremental.cluster_count,
                "bytes": incremental.byte_count,
            }
            self.store.log_operation(
                run_id=run_id, op="edit", doc_id=doc_id, version=new_version,
                success=True, detail=detail,
            )
            log_event("document_edited", doc_id=doc_id, version=new_version,
                      base_version=expected_version)
            return {
                "doc_id": doc_id,
                "version": new_version,
                "parent_version": expected_version,
                "build_mode": build_mode,
                "content_sha256": digest,
                "stats": compute_stats(incremental.text, new_raw).to_dict(),
            }
        except Exception as exc:
            self._log_failure("edit", run_id, doc_id, expected_version, detail, exc)
            raise

    def _decode_b64_utf8(self, b64: str) -> str:
        try:
            raw = base64.b64decode(b64.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
            raise InvalidBase64Error(
                "replacement 不是合法标准 base64", details={"reason": str(exc)}
            ) from exc
        return decode_strict(raw)

    # ── 查询 ────────────────────────────────────────────────────────────────
    def get_version(self, doc_id: str, version: int | None) -> dict[str, Any]:
        self._require_document(doc_id)
        if version is None:
            doc = self.store.get_document(doc_id)
            assert doc is not None
            version = doc["current_version"]
        raw = self._get_raw(doc_id, version)
        loaded = self._load(doc_id, version, raw)
        idx = loaded.index
        return {
            "doc_id": doc_id,
            "version": version,
            "content_sha256": loaded.content_sha256,
            "gcb_table_version": loaded.gcb_table_version,
            "unidata_version": loaded.unidata_version,
            "build_mode": loaded.build_mode,
            "stats": compute_stats(idx.text, raw).to_dict(),
            "lengths": {
                "bytes": idx.byte_count,
                "codepoints": idx.cp_count,
                "clusters": idx.cluster_count,
            },
        }

    def convert_position(
        self, doc_id: str, version: int | None, position: int, source: str, target: str
    ) -> dict[str, Any]:
        self._require_document(doc_id)
        if version is None:
            doc = self.store.get_document(doc_id)
            assert doc is not None
            version = doc["current_version"]
        raw = self._get_raw(doc_id, version)
        loaded = self._load(doc_id, version, raw)
        idx = loaded.index
        result = idx.convert(position, source, target)
        return {
            "doc_id": doc_id,
            "version": version,
            "source_space": source,
            "target_space": target,
            "input_position": position,
            "output_position": result,
        }

    def list_versions(self, doc_id: str) -> dict[str, Any]:
        self._require_document(doc_id)
        rows = self.store.list_versions(doc_id)
        return {
            "doc_id": doc_id,
            "versions": [
                {
                    "version": r["version"],
                    "parent_version": r["parent_version"],
                    "build_mode": r["build_mode"],
                    "content_sha256": r["content_sha256"],
                    "gcb_table_version": r["gcb_table_version"],
                    "unidata_version": r["unidata_version"],
                    "lengths": {
                        "bytes": r["byte_count"],
                        "codepoints": r["cp_count"],
                        "clusters": r["cluster_count"],
                    },
                }
                for r in rows
            ],
        }

    def diagnose_clusters(
        self, doc_id: str, version: int | None, limit: int | None
    ) -> dict[str, Any]:
        self._require_document(doc_id)
        if version is None:
            doc = self.store.get_document(doc_id)
            assert doc is not None
            version = doc["current_version"]
        raw = self._get_raw(doc_id, version)
        loaded = self._load(doc_id, version, raw)
        return {
            "doc_id": doc_id,
            "version": version,
            "content_sha256": loaded.content_sha256,
            "total_clusters": loaded.index.cluster_count,
            "returned": min(loaded.index.cluster_count, limit) if limit else loaded.index.cluster_count,
            "clusters": describe_clusters(loaded.index, limit),
        }

    def diagnose_versions(self) -> list[dict[str, Any]]:
        rows = self.store.recent_operations(100)
        return [
            {
                "run_id": r["run_id"],
                "ts": r["ts"],
                "op": r["op"],
                "doc_id": r["doc_id"],
                "version": r["version"],
                "success": bool(r["success"]),
                "error_category": r["error_category"],
                "error_code": r["error_code"],
                "detail": r["detail"],
            }
            for r in rows
        ]

    def replay_run(self, run_id: str) -> dict[str, Any]:
        import json
        rows = self.store.find_operations(run_id)
        return {
            "run_id": run_id,
            "events": [
                {
                    "ts": r["ts"],
                    "op": r["op"],
                    "doc_id": r["doc_id"],
                    "version": r["version"],
                    "success": bool(r["success"]),
                    "error_category": r["error_category"],
                    "error_code": r["error_code"],
                    "detail": json.loads(r["detail"]),
                }
                for r in rows
            ],
        }

    # ── 失败日志（四类错误可区分）────────────────────────────────────────────
    def _log_failure(
        self,
        op: str,
        run_id: str,
        doc_id: str | None,
        version: int | None,
        detail: dict[str, Any],
        exc: Exception,
    ) -> None:
        from .errors import IndexServiceError

        if isinstance(exc, IndexServiceError):
            category = exc.category.value
            code = exc.code
            detail["error_detail"] = exc.details
        else:  # 未预期异常归计算失败，保留 traceback 便于重放
            category = "computation_failure"
            code = "UNEXPECTED_ERROR"
            detail["exception"] = f"{type(exc).__name__}: {exc}"
        detail["phase"] = detail.get("phase", "unknown")
        self.store.log_operation(
            run_id=run_id, op=op, doc_id=doc_id, version=version,
            success=False, detail=detail,
            error_category=category, error_code=code,
        )
        log_event(
            f"{op}_failed", level=40,  # logging.WARNING
            run_id=run_id, doc_id=doc_id, error_category=category,
            error_code=code,
        )
