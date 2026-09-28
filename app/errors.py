"""错误契约。

所有跨模块错误都使用 :class:`SegmentError`，并归入 :data:`ERROR_CATEGORIES`
中的**互不相交**的类别。HTTP 层据此映射状态码，测试据此断言“失败类别”。

类别清单（对应需求：输入错误 / 状态冲突 / 资源耗尽 / 计算失败可区分）：

============================== ========= =====================
code                           HTTP      含义
============================== ========= =====================
INVALID_ARGUMENT               400       请求参数非法（阈值、时长、配置）
MEDIA_PARSE_ERROR              400       媒体无法解析或格式不受支持
EMPTY_INPUT                    400       媒体可解析但不含任何样本
RESOURCE_EXHAUSTED             413       超过样本数 / 字节数上限
STATE_CONFLICT                 409       作业状态不允许该操作
JOB_NOT_FOUND                  404       作业 / 运行不存在
COMPUTATION_FAILED             422       信号内核遇到无法计算的样本（NaN/Inf）
INTERNAL                       500       未归类内部错误
============================== ========= =====================
"""

from __future__ import annotations

from typing import Any

# 错误码 -> 粗类别。粗类别即日志中记录的 failure_category。
ERROR_CATEGORIES: dict[str, str] = {
    "INVALID_ARGUMENT": "input",
    "MEDIA_PARSE_ERROR": "input",
    "EMPTY_INPUT": "input",
    "RESOURCE_EXHAUSTED": "resource",
    "STATE_CONFLICT": "state",
    "JOB_NOT_FOUND": "state",
    "COMPUTATION_FAILED": "computation",
    "INTERNAL": "internal",
}

ERROR_HTTP_STATUS: dict[str, int] = {
    "INVALID_ARGUMENT": 400,
    "MEDIA_PARSE_ERROR": 400,
    "EMPTY_INPUT": 400,
    "RESOURCE_EXHAUSTED": 413,
    "STATE_CONFLICT": 409,
    "JOB_NOT_FOUND": 404,
    "COMPUTATION_FAILED": 422,
    "INTERNAL": 500,
}


class SegmentError(Exception):
    """跨模块统一异常。

    Attributes:
        code: 上表中的错误码。
        message: 面向调用方的可读说明（不含栈）。
        details: 结构化上下文，写入运行日志以便重放。
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        if code not in ERROR_CATEGORIES:
            code = "INTERNAL"
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details

    @property
    def category(self) -> str:
        return ERROR_CATEGORIES[self.code]

    @property
    def http_status(self) -> int:
        return ERROR_HTTP_STATUS[self.code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "category": self.category,
                "message": self.message,
                "details": self.details,
            }
        }
