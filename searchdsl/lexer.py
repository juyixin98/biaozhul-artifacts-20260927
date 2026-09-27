"""词法分析：把查询文本切成带列位置的 token 流。

规则见 docs/spec.md。要点：
- AND/OR/NOT 仅在以大写关键字形式独立出现时是运算符（小写 and/or/not 是普通词）；
- 引号内的一切都不按运算符解析；
- 反斜杠转义紧随字符：`` \\\" `` -> `` " ``、`` \\\\ `` -> `` \\ ``、`` \\: `` -> `` : ``；
- 所有位置均为 1 起始列号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from .errors import DslError, ErrorCategory

_SPECIAL = set('():"')
# 短语内容与索引端使用同一词元口径：[a-z0-9]+，小写化
_PHRASE_TOKEN_RE = re.compile(r"[a-z0-9]+")


class TokenKind(str, Enum):
    TERM = "TERM"
    PHRASE = "PHRASE"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COLON = "COLON"
    EOF = "EOF"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    col: int                    # 1 起始列号
    end: int                    # 独占结束列号
    value: Optional[str] = None       # TERM 的文本
    phrase: Optional[Tuple[str, ...]] = None  # PHRASE 切词结果

    @property
    def is_operand(self) -> bool:
        return self.kind in (TokenKind.TERM, TokenKind.PHRASE)


def _unescape(text: str, start_col: int) -> str:
    """处理裸词中的反斜杠转义。start_col 仅用于定位悬空转义。"""
    out: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text):
                raise DslError(
                    ErrorCategory.LEXER,
                    "悬空反斜杠：结尾的 '\\' 后缺少被转义字符",
                    position=start_col + i,
                )
            out.append(text[i + 1])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def lex(text: str) -> List[Token]:
    tokens: List[Token] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        col = i + 1

        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(Token(TokenKind.LPAREN, col, col + 1))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token(TokenKind.RPAREN, col, col + 1))
            i += 1
            continue
        if ch == ":":
            tokens.append(Token(TokenKind.COLON, col, col + 1))
            i += 1
            continue
        if ch == '"':
            phrase_text, close_idx = _read_phrase(text, i)
            terms = tuple(_PHRASE_TOKEN_RE.findall(phrase_text.lower()))
            if not terms:
                raise DslError(
                    ErrorCategory.LEXER,
                    "空短语 \"\"：短语至少要包含一个可检索词",
                    position=col,
                )
            tokens.append(Token(TokenKind.PHRASE, col, close_idx + 2, phrase=terms))
            i = close_idx + 1  # 跳过闭引号
            continue

        # 裸词：读到空白或特殊字符（转义字符除外）
        raw_start = i
        while i < n and not text[i].isspace():
            c = text[i]
            if c == "\\":
                if i + 1 >= n:
                    raise DslError(
                        ErrorCategory.LEXER,
                        "悬空反斜杠：结尾的 '\\' 后缺少被转义字符",
                        position=i + 1,
                    )
                i += 2  # 连同被转义字符一起跳过
                continue
            if c in _SPECIAL:
                break
            i += 1
        raw = text[raw_start:i]
        value = _unescape(raw, raw_start + 1)
        if value in ("AND", "OR", "NOT"):
            kind = TokenKind(value)
            tokens.append(Token(kind, col, col + len(value)))
        else:
            tokens.append(Token(TokenKind.TERM, col, col + len(raw), value=value))

    tokens.append(Token(TokenKind.EOF, n + 1, n + 1))
    return tokens


def _read_phrase(text: str, quote_idx: int) -> Tuple[str, int]:
    """从开引号位置读取短语，返回（已处理转义的文本, 闭引号在 text 中的下标）。"""
    out: List[str] = []
    i = quote_idx + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 >= n:
                raise DslError(
                    ErrorCategory.LEXER,
                    "悬空反斜杠：结尾的 '\\' 后缺少被转义字符",
                    position=i + 1,
                )
            out.append(text[i + 1])  # \x -> x，引号内符号失去特殊含义
            i += 2
            continue
        if c == '"':
            return "".join(out), i
        out.append(c)
        i += 1
    raise DslError(
        ErrorCategory.LEXER,
        f"未闭合的引号：位置 {quote_idx + 1} 的引号缺少右引号",
        position=quote_idx + 1,
    )
