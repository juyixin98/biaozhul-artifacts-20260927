"""错误分类层。

绝不把异常或未知状态统一返回成功。每类可预期失败都有显式错误码与
HTTP 状态；未预期异常单独归类为 ``INTERNAL_ERROR``（500），不吞掉。
"""

from __future__ import annotations

from enum import Enum


class FailureCode(str, Enum):
    # --- 输入解析/规则校验（422） ---
    INVALID_INPUT = "INVALID_INPUT"
    EMPTY_DATASET = "EMPTY_DATASET"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    DUPLICATE_COLUMN = "DUPLICATE_COLUMN"
    NO_QUASI_IDENTIFIER = "NO_QUASI_IDENTIFIER"
    NO_SENSITIVE = "NO_SENSITIVE"
    DUPLICATE_ROW_ID = "DUPLICATE_ROW_ID"
    INVALID_THRESHOLD = "INVALID_THRESHOLD"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    COLUMN_LIMIT_EXCEEDED = "COLUMN_LIMIT_EXCEEDED"
    # --- 泛化层级（422） ---
    INVALID_HIERARCHY = "INVALID_HIERARCHY"
    HIERARCHY_NOT_COVERING = "HIERARCHY_NOT_COVERING"
    HIERARCHY_NOT_MONOTONE = "HIERARCHY_NOT_MONOTONE"
    HIERARCHY_TOO_DEEP = "HIERARCHY_TOO_DEEP"
    MISSING_HIERARCHY = "MISSING_HIERARCHY"
    # --- 分析失败（422，属于明确的业务判定而非成功） ---
    K_UNREACHABLE = "K_UNREACHABLE"
    L_UNREACHABLE = "L_UNREACHABLE"
    # --- 资源/状态（404/409） ---
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    SCHEMA_NOT_FOUND = "SCHEMA_NOT_FOUND"
    STATE_CONFLICT = "STATE_CONFLICT"
    # --- 安全 ---
    DECRYPTION_FAILED = "DECRYPTION_FAILED"
    # --- 其他 ---
    INTERNAL_ERROR = "INTERNAL_ERROR"


# 失败类别：用于测试断言与日志统计，明确区分"输入问题/不可达/系统问题"
CATEGORY = {
    FailureCode.INVALID_INPUT: "validation",
    FailureCode.EMPTY_DATASET: "validation",
    FailureCode.UNKNOWN_COLUMN: "validation",
    FailureCode.DUPLICATE_COLUMN: "validation",
    FailureCode.NO_QUASI_IDENTIFIER: "validation",
    FailureCode.NO_SENSITIVE: "validation",
    FailureCode.DUPLICATE_ROW_ID: "validation",
    FailureCode.INVALID_THRESHOLD: "validation",
    FailureCode.ROW_LIMIT_EXCEEDED: "validation",
    FailureCode.COLUMN_LIMIT_EXCEEDED: "validation",
    FailureCode.INVALID_HIERARCHY: "hierarchy",
    FailureCode.HIERARCHY_NOT_COVERING: "hierarchy",
    FailureCode.HIERARCHY_NOT_MONOTONE: "hierarchy",
    FailureCode.HIERARCHY_TOO_DEEP: "hierarchy",
    FailureCode.MISSING_HIERARCHY: "hierarchy",
    FailureCode.K_UNREACHABLE: "threshold_unreachable",
    FailureCode.L_UNREACHABLE: "threshold_unreachable",
    FailureCode.RUN_NOT_FOUND: "not_found",
    FailureCode.SCHEMA_NOT_FOUND: "not_found",
    FailureCode.STATE_CONFLICT: "conflict",
    FailureCode.DECRYPTION_FAILED: "security",
    FailureCode.INTERNAL_ERROR: "internal",
}

HTTP_STATUS = {
    FailureCode.RUN_NOT_FOUND: 404,
    FailureCode.SCHEMA_NOT_FOUND: 404,
    FailureCode.STATE_CONFLICT: 409,
}


class ServiceError(Exception):
    """业务错误基类：携带稳定错误码、面向调用方的消息与结构化详情。"""

    def __init__(
        self,
        code: FailureCode,
        message: str,
        details: dict | None = None,
        *,
        internal: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.internal = internal

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 422)

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code.value,
                "category": CATEGORY[self.code],
                "message": self.message,
                "details": self.details,
            }
        }
