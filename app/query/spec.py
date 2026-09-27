"""查询文本规范（text specification）。

语法（EBNF，运算符大小写不敏感）::

    expr   := or_expr
    or_expr   := and_expr ( OR and_expr )*
    and_expr  := not_factor ( AND not_factor )*
    not_factor := NOT not_factor | atom
    atom   := TERM | "(" expr ")"

省略运算符时不做隐式 AND（避免歧义）；NOT 是一元前缀运算符。
TERM 由字母/数字/下划线组成，也支持双引号 "term a" 形式。

示例::

    cat AND dog
    cat AND NOT dog
    (cat OR dog) AND NOT fish
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class ErrorCategory(str, Enum):
    PARSE_ERROR = "parse_error"
    UNKNOWN_TERM = "unknown_term"
    VERSION_NOT_FOUND = "version_not_found"
    VERSION_CONFLICT = "version_conflict"
    UNIVERSE_NOT_VERSIONED = "universe_not_versioned"
    INTERNAL = "internal"


class QueryError(ValueError):
    """查询文本本身的失败（默认分类为 parse_error）。"""

    category: ErrorCategory = ErrorCategory.PARSE_ERROR


class TokenType(str, Enum):
    TERM = "term"
    AND = "and"
    OR = "or"
    NOT = "not"
    LPAREN = "lparen"
    RPAREN = "rparen"
    EOF = "eof"


@dataclass(frozen=True)
class Token:
    kind: TokenType
    text: str
    pos: int  # 在原始查询串中的起始位置，用于诊断定位


_KEYWORDS = {
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
}


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(Token(TokenType.LPAREN, ch, i))
            i += 1
            continue
        if ch == ")":
            tokens.append(Token(TokenType.RPAREN, ch, i))
            i += 1
            continue
        if ch == '"':
            start = i
            i += 1
            buf: List[str] = []
            while i < n and source[i] != '"':
                buf.append(source[i])
                i += 1
            if i >= n:
                raise QueryError(f"未闭合的引号（位置 {start}）")
            i += 1  # 跳过右引号
            text = "".join(buf).strip()
            if not text:
                raise QueryError(f"空词项（位置 {start}）")
            tokens.append(Token(TokenType.TERM, text, start))
            continue
        if ch.isalnum() or ch == "_":
            start = i
            buf2: List[str] = []
            while i < n and (source[i].isalnum() or source[i] == "_"):
                buf2.append(source[i])
                i += 1
            text = "".join(buf2)
            kind = _KEYWORDS.get(text.lower(), TokenType.TERM)
            tokens.append(Token(kind, text, start))
            continue
        raise QueryError(f"非法字符 {ch!r}（位置 {i}）")
    tokens.append(Token(TokenType.EOF, "", n))
    return tokens


# ---------------- AST ----------------


@dataclass(frozen=True)
class Node:
    """所有 AST 节点的基类；pos 指向触发该节点的首个 token，便于诊断。"""

    pos: int = 0


@dataclass(frozen=True)
class Term(Node):
    term: str = ""


@dataclass(frozen=True)
class Not(Node):
    child: Node = None  # type: ignore[assignment]


@dataclass(frozen=True)
class And(Node):
    children: tuple = ()


@dataclass(frozen=True)
class Or(Node):
    children: tuple = ()


class Parser:
    """递归下降解析器（见模块 docstring 中的 EBNF）。"""

    def __init__(self, tokens: List[Token], source: str):
        self._tokens = tokens
        self._source = source
        self._i = 0

    @property
    def _cur(self) -> Token:
        return self._tokens[self._i]

    def _advance(self) -> Token:
        tok = self._tokens[self._i]
        if self._i < len(self._tokens) - 1:
            self._i += 1
        return tok

    def parse(self) -> Node:
        if self._cur.kind is TokenType.EOF:
            raise QueryError("空查询")
        node = self._parse_or()
        if self._cur.kind is not TokenType.EOF:
            raise QueryError(
                f"右括号不匹配或多余 token {self._cur.text!r}（位置 {self._cur.pos}）"
            )
        return node

    def _parse_or(self) -> Node:
        left = self._parse_and()
        children: List[Node] = [left]
        while self._cur.kind is TokenType.OR:
            op = self._advance()
            right = self._parse_and()
            children.append(right)
            left = Or(pos=op.pos, children=tuple(children))
        return left

    def _parse_and(self) -> Node:
        left = self._parse_not()
        children: List[Node] = [left]
        while self._cur.kind is TokenType.AND:
            op = self._advance()
            right = self._parse_not()
            children.append(right)
            left = And(pos=op.pos, children=tuple(children))
        return left

    def _parse_not(self) -> Node:
        if self._cur.kind is TokenType.NOT:
            tok = self._advance()
            child = self._parse_not()
            return Not(pos=tok.pos, child=child)
        return self._parse_atom()

    def _parse_atom(self) -> Node:
        tok = self._cur
        if tok.kind is TokenType.TERM:
            self._advance()
            return Term(pos=tok.pos, term=tok.text)
        if tok.kind is TokenType.LPAREN:
            self._advance()
            node = self._parse_or()
            if self._cur.kind is not TokenType.RPAREN:
                raise QueryError(f"缺少右括号（左括号位置 {tok.pos}）")
            self._advance()
            return node
        if tok.kind is TokenType.EOF:
            raise QueryError("查询在期待词项或括号处结束")
        raise QueryError(f"意外的 token {tok.text!r}（位置 {tok.pos}）")


def parse_query(source: str) -> Node:
    """把查询文本解析为 AST；失败抛 QueryError。"""
    if source is None or not source.strip():
        raise QueryError("空查询")
    tokens = tokenize(source)
    return Parser(tokens, source).parse()


def collect_terms(node: Node, out: Optional[List[str]] = None) -> List[str]:
    """按首次出现顺序收集 AST 中引用的全部词项。"""
    if out is None:
        out = []
    if isinstance(node, Term):
        if node.term not in out:
            out.append(node.term)
    elif isinstance(node, (And, Or)):
        for c in node.children:
            collect_terms(c, out)
    elif isinstance(node, Not):
        collect_terms(node.child, out)
    return out
