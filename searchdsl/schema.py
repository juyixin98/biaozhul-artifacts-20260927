"""字段白名单与执行前类型校验。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from . import ast_nodes as ast
from .errors import DslError, ErrorCategory

INT_LITERAL = re.compile(r"^[+-]?\d+$")


@dataclass(frozen=True)
class Schema:
    field_types: Dict[str, str]        # 字段名 -> text / int
    default_fields: List[str]          # 不限定字段时检索的文本字段

    @property
    def fields(self) -> List[str]:
        return list(self.field_types)

    def validate(self, node: ast.Node) -> None:
        """整树执行前校验：未知字段、int 字段非整数值、非文本字段上的短语。"""
        if isinstance(node, (ast.Term, ast.Phrase)):
            if node.field is None:
                return  # 默认字段全部是 text，短语合法
            if node.field not in self.field_types:
                raise DslError(
                    ErrorCategory.FIELD_UNKNOWN,
                    f"未知字段 '{node.field}'：白名单字段为 "
                    f"{', '.join(sorted(self.field_types))}",
                    position=node.pos,
                )
            ftype = self.field_types[node.field]
            if ftype == "int" and isinstance(node, ast.Phrase):
                raise DslError(
                    ErrorCategory.FIELD_TYPE,
                    f"字段 '{node.field}' 是整数类型，不能接短语查询",
                    position=node.pos,
                )
            if ftype == "int" and isinstance(node, ast.Term):
                if not INT_LITERAL.match(node.value):
                    raise DslError(
                        ErrorCategory.FIELD_TYPE,
                        f"字段 '{node.field}' 是整数类型，值 '{node.value}' 不是合法整数",
                        position=node.pos,
                    )
            return
        if isinstance(node, ast.Not):
            self.validate(node.child)
        elif isinstance(node, (ast.And, ast.Or)):
            for child in node.children:
                self.validate(child)
