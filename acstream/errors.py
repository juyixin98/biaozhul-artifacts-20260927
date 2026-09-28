"""错误码与 API 异常定义。

错误类别（outcome）分三类，供诊断模块记录：
- accept：请求被正常接受并处理；
- reject：服务端可以确定请求非法，明确拒绝；
- undetermined：服务端无法判定状态是否仍然安全一致，拒绝继续并要求客户端澄清。
"""

from __future__ import annotations


class ErrorCode:
    # 请求体结构 / 字段类型错误（Pydantic 校验失败）
    VALIDATION_ERROR = "VALIDATION_ERROR"
    # 载荷编码无法解码（非法 base64/hex/utf-8）
    ENCODING_ERROR = "ENCODING_ERROR"
    # 模式为空字节串
    EMPTY_PATTERN = "EMPTY_PATTERN"
    # 模式集合为空
    EMPTY_PATTERN_SET = "EMPTY_PATTERN_SET"
    # 同一版本内 pattern_id 重复
    DUPLICATE_PATTERN_ID = "DUPLICATE_PATTERN_ID"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    # 会话已 finish，不允许再喂数据
    SESSION_FINISHED = "SESSION_FINISHED"
    # 客户端声明的 expected_offset 与服务端不一致，本次块未被消费
    OFFSET_MISMATCH = "OFFSET_MISMATCH"
    # 分页游标签名非法、跨会话使用或内容被篡改
    CURSOR_INVALID = "CURSOR_INVALID"
    LIMIT_INVALID = "LIMIT_INVALID"
    # 客户端指纹与当前自动机不一致，无法判定节点语义，需显式边界处理
    FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(Exception):
    """所有可预期的业务/协议错误统一抛出本异常。"""

    def __init__(
        self,
        code: str,
        http_status: int,
        message: str,
        *,
        details: dict | None = None,
        outcome: str = "reject",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.details = details or {}
        self.outcome = outcome
