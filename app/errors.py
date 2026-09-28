"""错误类型与错误码。

关键纪律：异常或未知状态绝不能被统一包装成成功响应。每类错误有固定的
机器可读错误码（``E_*``）、HTTP 状态码与 HTTP 响应体结构，README 中有完整语义表。
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有可预期业务错误的基类。"""

    code = "E_INTERNAL"
    status_code = 500
    category = "internal"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_body(self, request_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "category": self.category,
                "message": self.message,
                "details": self.details,
            },
        }
        if request_id is not None:
            body["request_id"] = request_id
        return body


class InvalidSurface(AppError):
    """显示原文为空、规范化后为空，或包含禁止字符（NUL）。"""

    code = "E_INVALID_SURFACE"
    status_code = 400
    category = "invalid_input"


class InvalidScore(AppError):
    """词频不是非负整数。"""

    code = "E_INVALID_SCORE"
    status_code = 400
    category = "invalid_input"


class InvalidLimit(AppError):
    """top-k 的 k 不在 1..1000。"""

    code = "E_INVALID_LIMIT"
    status_code = 400
    category = "invalid_input"


class EntryNotFound(AppError):
    """按 id 删除/增减词频时词条不存在。"""

    code = "E_ENTRY_NOT_FOUND"
    status_code = 404
    category = "not_found"


class DuplicateId(AppError):
    """批量写入中同一 id 映射到不同显示原文，或 id 冲突。"""

    code = "E_DUPLICATE_ID"
    status_code = 409
    category = "conflict"


class NormalizerVersionMismatch(AppError):
    """磁盘索引的规范化版本与当前二进制不一致——必须重建索引。"""

    code = "E_NORMALIZER_VERSION_MISMATCH"
    status_code = 409
    category = "version_mismatch"


class SnapshotConflict(AppError):
    """同名快照已存在。"""

    code = "E_SNAPSHOT_CONFLICT"
    status_code = 409
    category = "conflict"


class SnapshotNotFound(AppError):
    code = "E_SNAPSHOT_NOT_FOUND"
    status_code = 404
    category = "not_found"


class IndexCorrupt(AppError):
    """子树上界/结构不变量校验失败，或快照文件校验不过。绝不伪装成功。"""

    code = "E_INDEX_CORRUPT"
    status_code = 409
    category = "integrity"


# 测试中用于按类别断言失败原因的稳定类别集合。
ERROR_CODES = frozenset(
    {
        "E_INVALID_INPUT",
        "E_INVALID_SURFACE",
        "E_INVALID_SCORE",
        "E_INVALID_LIMIT",
        "E_ENTRY_NOT_FOUND",
        "E_DUPLICATE_ID",
        "E_NORMALIZER_VERSION_MISMATCH",
        "E_SNAPSHOT_CONFLICT",
        "E_SNAPSHOT_NOT_FOUND",
        "E_INDEX_CORRUPT",
        "E_INTERNAL",
    }
)
