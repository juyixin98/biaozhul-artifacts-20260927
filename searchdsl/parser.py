"""递归下降语法分析。

文法（优先级 NOT > AND（含隐式 AND）> OR）::

    or_expr   := and_expr (OR and_expr)*
    and_expr  := unary ((AND)? unary)*      # 相邻操作数之间默认 AND
    unary     := NOT unary | primary
    primary   := LPAREN or_expr RPAREN | (TERM COLON)? (TERM | PHRASE)

字段限定 ``field:value`` 要求冒号两侧紧邻（不允许空白）。
"""

from __future__ import annotations

from typing import List, Optional

from . import ast_nodes as ast
from .errors import DslError, ErrorCategory
from .lexer import Token, TokenKind, lex

_OPERATOR_KINDS = {TokenKind.AND, TokenKind.OR, TokenKind.NOT}


class _Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.i = 0

    # -- 基础工具 ---------------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        if self.i < len(self.tokens) - 1:
            self.i += 1
        return tok

    def _error(self, message: str, token: Optional[Token] = None) -> DslError:
        tok = token if token is not None else self.cur
        return DslError(ErrorCategory.PARSE, message, position=tok.col)

    # -- 文法 -------------------------------------------------------------
    def parse(self) -> ast.Node:
        if self.cur.kind is TokenKind.EOF:
            return ast.Empty()
        node = self.parse_or()
        if self.cur.kind is not TokenKind.EOF:
            raise self._error(
                f"意外的 token：{self.cur.kind.value}，查询在完整表达式后还有多余内容"
            )
        return node

    def parse_or(self) -> ast.Node:
        left = self.parse_and()
        children = [left]
        while self.cur.kind is TokenKind.OR:
            self.advance()
            children.append(self.parse_and())
        return children[0] if len(children) == 1 else ast.Or(tuple(children))

    def parse_and(self) -> ast.Node:
        children = [self.parse_unary()]
        while True:
            tok = self.cur
            if tok.kind is TokenKind.AND:
                self.advance()
                children.append(self.parse_unary())
            elif tok.kind in (TokenKind.LPAREN, TokenKind.TERM,
                              TokenKind.PHRASE, TokenKind.NOT):
                # 相邻操作数：隐式 AND
                children.append(self.parse_unary())
            else:
                break
        return children[0] if len(children) == 1 else ast.And(tuple(children))

    def parse_unary(self) -> ast.Node:
        tok = self.cur
        if tok.kind is TokenKind.NOT:
            self.advance()
            return ast.Not(self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> ast.Node:
        tok = self.cur
        if tok.kind is TokenKind.LPAREN:
            open_tok = tok
            self.advance()
            if self.cur.kind is TokenKind.RPAREN:
                raise self._error("空括号：'(' 与 ')' 之间缺少表达式")
            inner = self.parse_or()
            if self.cur.kind is not TokenKind.RPAREN:
                raise DslError(
                    ErrorCategory.PARSE,
                    f"括号未闭合：位置 {open_tok.col} 的 '(' 缺少 ')'",
                    position=open_tok.col,
                )
            self.advance()
            return inner
        return self.parse_operand()

    def parse_operand(self) -> ast.Node:
        tok = self.cur

        if tok.kind in _OPERATOR_KINDS:
            raise self._error(
                f"运算符 {tok.kind.value} 位置非法：这里需要一个查询词或短语"
            )
        if tok.kind in (TokenKind.RPAREN, TokenKind.COLON):
            raise self._error(f"意外的 token：{tok.kind.value}")
        if tok.kind is TokenKind.EOF:
            raise self._error("查询意外结束：这里需要一个查询词或短语")

        # 字段限定：TERM COLON (TERM | PHRASE)，冒号两侧必须紧邻
        field_name: Optional[str] = None
        if tok.kind is TokenKind.TERM and self.tokens[self.i + 1].kind is TokenKind.COLON:
            colon = self.tokens[self.i + 1]
            if colon.col != tok.end:
                raise DslError(
                    ErrorCategory.PARSE,
                    "字段限定符 ':' 左侧不允许有空白，应为 field:value",
                    position=colon.col,
                )
            value_tok = self.tokens[self.i + 2]
            if value_tok.col != colon.end:
                raise DslError(
                    ErrorCategory.PARSE,
                    "字段限定符 ':' 右侧不允许有空白，应为 field:value",
                    position=colon.col,
                )
            if value_tok.kind not in (TokenKind.TERM, TokenKind.PHRASE):
                raise DslError(
                    ErrorCategory.PARSE,
                    f"字段 '{tok.value}' 缺少有效限定值（冒号后需要词或短语）",
                    position=colon.end,
                )
            field_name = tok.value
            self.advance()  # TERM
            self.advance()  # COLON
            value_tok = self.cur
            value_pos = tok.col  # 字段类错误定位在字段名起始列
        else:
            if tok.kind not in (TokenKind.TERM, TokenKind.PHRASE):
                raise self._error(f"意外的 token：{tok.kind.value}")
            value_tok = tok
            value_pos = tok.col

        self.advance()
        if value_tok.kind is TokenKind.TERM:
            return ast.Term(value=value_tok.value, field=field_name, pos=value_pos)
        return ast.Phrase(terms=value_tok.phrase, field=field_name, pos=value_pos)


def parse(text: str) -> ast.Node:
    return _Parser(lex(text)).parse()
