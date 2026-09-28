"""错误分类：每类业务失败都有稳定错误码，绝不把未知状态包装成成功。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """稳定错误码（写进 HTTP 错误体与审计日志），测试按码断言。"""

    # —— 解析 / 输入 ——
    EMPTY_PAYLOAD = "EMPTY_PAYLOAD"
    EMPTY_DATA = "EMPTY_DATA"
    EMPTY_COLUMNS = "EMPTY_COLUMNS"
    EMPTY_QUASI_IDENTIFIERS = "EMPTY_QUASI_IDENTIFIERS"
    EMPTY_SENSITIVE = "EMPTY_SENSITIVE"
    COLUMN_NOT_FOUND = "COLUMN_NOT_FOUND"
    DUPLICATE_COLUMN_ROLE = "DUPLICATE_COLUMN_ROLE"
    ROW_WIDTH_MISMATCH = "ROW_WIDTH_MISMATCH"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    HEADER_MISSING_COLUMNS = "HEADER_MISSING_COLUMNS"

    # —— 泛化层级 ——
    HIERARCHY_MISSING = "HIERARCHY_MISSING"
    HIERARCHY_LEVEL_NOT_FOUND = "HIERARCHY_LEVEL_NOT_FOUND"
    HIERARCHY_NOT_CONTAINING = "HIERARCHY_NOT_CONTAINING"
    HIERARCHY_INCOMPLETE = "HIERARCHY_INCOMPLETE"
    HIERARCHY_BAD_LEVEL = "HIERARCHY_BAD_LEVEL"
    HIERARCHY_DUPLICATE_LABEL = "HIERARCHY_DUPLICATE_LABEL"
    HIERARCHY_UNKNOWN_KEY = "HIERARCHY_UNKNOWN_KEY"
    HIERARCHY_KEEP_NOT_DECREASING = "HIERARCHY_KEEP_NOT_DECREASING"

    # —— 优化 ——
    THRESHOLD_UNREACHABLE = "THRESHOLD_UNREACHABLE"
    LATTICE_TOO_LARGE = "LATTICE_TOO_LARGE"

    # —— 运行 / 存储 / 访问控制 ——
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    RUN_FORBIDDEN = "RUN_FORBIDDEN"
    UNAUTHORIZED = "UNAUTHORIZED"
    STATE_ERROR = "STATE_ERROR"

    # —— 兜底 ——
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RiskError(Exception):
    """业务异常基类：携带稳定错误码、对外安全消息与机器可读细节。

    细节字典只允许放聚合/结构信息，禁止放原始准标识符或敏感值——
    该约束由 :mod:`anon_risk.security.view` 的出站白名单再次强制。
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 400

    def __init__(self, message: str, code: ErrorCode | None = None,
                 details: dict | None = None, http_status: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            }
        }
