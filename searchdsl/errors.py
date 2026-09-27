"""DSL 错误类型：所有失败都带类别与（可选的）1 起始列位置。"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class ErrorCategory(str, Enum):
    LEXER = "LEXER_ERROR"            # 词法错误（未闭合引号、悬空转义…）
    PARSE = "PARSE_ERROR"            # 语法错误（括号不平衡、运算符位置非法…）
    FIELD_UNKNOWN = "FIELD_UNKNOWN"  # 字段不在白名单
    FIELD_TYPE = "FIELD_TYPE"        # 字段值类型不匹配 / 非文本字段上的短语
    BUDGET = "BUDGET_EXCEEDED"       # 复杂度预算超限


class DslError(Exception):
    """携带类别与列位置的 DSL 错误。position 为 1 起始列号，可为 None。"""

    def __init__(self, category: ErrorCategory, message: str,
                 position: Optional[int] = None) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.position = position

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "message": self.message,
            "position": self.position,
        }
