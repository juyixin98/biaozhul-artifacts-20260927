"""查询文本规范：布尔查询的词法与语法。

语法（显式括号优先，NOT 优先级最高，AND 高于 OR；不允许隐式 AND）：

    expr    := or_expr
    or_expr := and_expr ( OR and_expr )*
    and_expr := not_expr ( AND not_expr )*
    not_expr := NOT not_expr | atom
    atom    := TERM | '*' | '(' expr ')'

关键字 AND / OR / NOT 大小写不敏感；TERM 与文档分词同规则（\\w+）。
`*` 表示当前版本的显式文档全集（universe），它必须是有限的已索引文档集合，
NOT 永远相对于该全集求补，绝不补成无限整数集。

解析失败统一抛 SpecError，带字符位置 span 与失败类别（category），
便于接口把“失败原因”单列返回，而不是一个含糊的 400。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tokenize import tokenize

# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Term:
    value: str  # 已小写
    span: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class Universe:
    """显式文档全集原子 `*`。"""

    span: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class Not:
    child: "Node"
    span: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class And:
    children: tuple["Node", ...]
    span: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class Or:
    children: tuple["Node", ...]
    span: tuple[int, int] = (0, 0)


Node = Term | Universe | Not | And | Or

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------

CATEGORY_EMPTY = "empty_expression"
CATEGORY_UNEXPECTED_TOKEN = "unexpected_token"
CATEGORY_UNEXPECTED_END = "unexpected_end_of_input"
CATEGORY_UNMATCHED_OPEN = "unmatched_open_parenthesis"
CATEGORY_UNMATCHED_CLOSE = "unmatched_close_parenthesis"
CATEGORY_DANGLING_OPERATOR = "dangling_operator"

ALL_CATEGORIES = (
    CATEGORY_EMPTY,
    CATEGORY_UNEXPECTED_TOKEN,
    CATEGORY_UNEXPECTED_END,
    CATEGORY_UNMATCHED_OPEN,
    CATEGORY_UNMATCHED_CLOSE,
    CATEGORY_DANGLING_OPERATOR,
)


class SpecError(ValueError):
    """查询文本规范错误。category 是机器可读的失败类别，position 为字符偏移。"""

    def __init__(
        self,
        message: str,
        category: str,
        position: int,
        span: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.category = category
        self.position = position
        self.span = span or (position, position)


# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------

# 词素：( AND | OR | NOT | LPAREN | RPAREN | STAR | TERM )
_LEX_RE = re.compile(
    r"\s*(?:"
    r"(?P<AND>\bAND\b)|(?P<OR>\bOR\b)|(?P<NOT>\bNOT\b)"
    r"|(?P<LPAREN>\()|(?P<RPAREN>\))|(?P<STAR>\*)"
    r"|(?P<TERM>\w+))",
    re.IGNORECASE | re.UNICODE,
)

@dataclass(frozen=True)
class Tok:
    kind: str  # AND/OR/NOT/LPAREN/RPAREN/STAR/TERM
    value: str
    span: tuple[int, int]


def _lex(text: str) -> list[Tok]:
    toks: list[Tok] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _LEX_RE.match(text, pos)
        # \s* 可吃掉前导空白；若整体失配或零宽匹配，说明 pos 后是非法字符
        if m is None or (m.start() == m.end()):
            bad = pos
            while bad < n and text[bad].isspace():
                bad += 1
            if bad >= n:
                break  # 只剩尾部空白
            raise SpecError(
                f"位置 {bad} 处存在无法识别的字符 {text[bad]!r}",
                CATEGORY_UNEXPECTED_TOKEN,
                bad,
            )
        kind = m.lastgroup
        raw = m.group(kind)
        if kind in ("AND", "OR", "NOT"):
            kind = raw.upper()
            value = kind
        elif kind == "TERM":
            value = raw.lower()
        else:
            value = raw
        toks.append(Tok(kind, value, (m.start(), m.end())))
        pos = m.end()
    return toks


# ---------------------------------------------------------------------------
# 语法（递归下降）
# ---------------------------------------------------------------------------


@dataclass
class _Parser:
    text: str
    toks: list[Tok]
    i: int = 0

    def peek(self) -> Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self) -> Tok | None:
        t = self.peek()
        if t is not None:
            self.i += 1
        return t

    def parse(self) -> Node:
        if not self.toks:
            raise SpecError(
                "查询表达式为空",
                CATEGORY_EMPTY,
                0,
            )
        node = self.parse_or()
        if self.peek() is not None:
            t = self.peek()
            if t.kind == "RPAREN":
                raise SpecError(
                    "出现没有匹配左括号的右括号",
                    CATEGORY_UNMATCHED_CLOSE,
                    t.span[0],
                    t.span,
                )
            raise SpecError(
                f"位置 {t.span[0]} 处存在多余的词素 {t.value!r}（缺少 AND/OR 连接？）",
                CATEGORY_UNEXPECTED_TOKEN,
                t.span[0],
                t.span,
            )
        return node

    def parse_or(self) -> Node:
        first = self.parse_and()
        children = [first]
        while self.peek() is not None and self.peek().kind == "OR":
            op = self.next()
            if self.peek() is None or self.peek().kind in ("OR", "AND", "RPAREN"):
                t = self.peek()
                pos = t.span[0] if t else op.span[1]
                span = t.span if t else (op.span[1], op.span[1])
                raise SpecError(
                    "OR 之后缺少操作数",
                    CATEGORY_DANGLING_OPERATOR,
                    pos,
                    span,
                )
            children.append(self.parse_and())
        if len(children) == 1:
            return first
        return Or(tuple(children), (first.span[0], children[-1].span[1]))

    def parse_and(self) -> Node:
        first = self.parse_not()
        children = [first]
        while self.peek() is not None and self.peek().kind == "AND":
            op = self.next()
            if self.peek() is None or self.peek().kind in ("OR", "AND", "RPAREN"):
                t = self.peek()
                pos = t.span[0] if t else op.span[1]
                span = t.span if t else (op.span[1], op.span[1])
                raise SpecError(
                    "AND 之后缺少操作数",
                    CATEGORY_DANGLING_OPERATOR,
                    pos,
                    span,
                )
            children.append(self.parse_not())
        if len(children) == 1:
            return first
        return And(tuple(children), (first.span[0], children[-1].span[1]))

    def parse_not(self) -> Node:
        t = self.peek()
        if t is None:
            raise SpecError(
                "表达式意外结束",
                CATEGORY_UNEXPECTED_END,
                len(self.text),
            )
        if t.kind == "NOT":
            self.next()
            # NOT 后面必须能取到一个一元操作数
            nxt = self.peek()
            if nxt is None:
                raise SpecError(
                    "NOT 之后缺少操作数",
                    CATEGORY_UNEXPECTED_END,
                    t.span[1],
                    (t.span[1], t.span[1]),
                )
            if nxt.kind in ("AND", "OR", "RPAREN"):
                raise SpecError(
                    f"NOT 之后不能是 {nxt.value!r}",
                    CATEGORY_DANGLING_OPERATOR,
                    nxt.span[0],
                    nxt.span,
                )
            child = self.parse_not()
            return Not(child, (t.span[0], child.span[1]))
        return self.parse_atom()

    def parse_atom(self) -> Node:
        t = self.next()
        if t is None:
            raise SpecError(
                "表达式意外结束，期望 term、NOT 或括号",
                CATEGORY_UNEXPECTED_END,
                len(self.text),
            )
        if t.kind == "TERM":
            if t.value in ("and", "or", "not"):  # 理论上词法已吸收，双保险
                raise SpecError(
                    f"关键字 {t.value.upper()} 不能作为 term",
                    CATEGORY_UNEXPECTED_TOKEN,
                    t.span[0],
                    t.span,
                )
            return Term(t.value, t.span)
        if t.kind == "STAR":
            return Universe(t.span)
        if t.kind == "LPAREN":
            inner = self.parse_or()
            close = self.next()
            if close is None:
                raise SpecError(
                    "左括号没有匹配的右括号",
                    CATEGORY_UNMATCHED_OPEN,
                    t.span[0],
                    t.span,
                )
            if close.kind != "RPAREN":
                raise SpecError(
                    f"括号内表达式后出现 {close.value!r}，缺少右括号",
                    CATEGORY_UNMATCHED_OPEN,
                    close.span[0],
                    close.span,
                )
            return inner
        if t.kind == "RPAREN":
            raise SpecError(
                "出现没有匹配左括号的右括号",
                CATEGORY_UNMATCHED_CLOSE,
                t.span[0],
                t.span,
            )
        # AND / OR 出现在这里都属于悬空运算符
        raise SpecError(
            f"操作符 {t.value} 缺少左侧操作数",
            CATEGORY_DANGLING_OPERATOR,
            t.span[0],
            t.span,
        )


def parse_query(text: str) -> Node:
    """把查询字符串解析为 AST。空串/纯空白抛 SpecError(empty_expression)。"""
    if not isinstance(text, str):
        raise SpecError(
            f"查询必须是字符串，收到 {type(text).__name__}",
            CATEGORY_EMPTY,
            0,
        )
    toks = _lex(text)
    return _Parser(text=text, toks=toks).parse()


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------


def terms_in(node: Node) -> list[str]:
    """按出现顺序收集 AST 中引用的 term（不去重，由调用方处理）。"""
    out: list[str] = []

    def walk(n: Node) -> None:
        if isinstance(n, Term):
            out.append(n.value)
        elif isinstance(n, (And, Or)):
            for c in n.children:
                walk(c)
        elif isinstance(n, Not):
            walk(n.child)

    walk(node)
    return out


def canonical(node: Node) -> str:
    """把 AST 规范化成带最小括号的文本，用于展示“关键步骤”。"""
    if isinstance(node, Term):
        return node.value
    if isinstance(node, Universe):
        return "*"
    if isinstance(node, Not):
        inner = canonical(node.child)
        if isinstance(node.child, (And, Or)):
            inner = f"({inner})"
        return f"NOT {inner}"
    if isinstance(node, And):
        parts = [canonical(c) for c in node.children]
        return "(" + " AND ".join(parts) + ")"
    if isinstance(node, Or):
        parts = [canonical(c) for c in node.children]
        return "(" + " OR ".join(parts) + ")"
    raise TypeError(f"未知 AST 节点 {type(node)!r}")


def to_dict(node: Node) -> dict:
    """AST 序列化为 JSON 友好结构（含字符 span，便于诊断定位）。"""
    if isinstance(node, Term):
        return {"type": "term", "value": node.value, "span": list(node.span)}
    if isinstance(node, Universe):
        return {"type": "universe", "span": list(node.span)}
    if isinstance(node, Not):
        return {"type": "not", "child": to_dict(node.child), "span": list(node.span)}
    if isinstance(node, (And, Or)):
        kind = "and" if isinstance(node, And) else "or"
        return {
            "type": kind,
            "children": [to_dict(c) for c in node.children],
            "span": list(node.span),
        }
    raise TypeError(f"未知 AST 节点 {type(node)!r}")


def grammar_spec() -> dict:
    """返回文本规范说明（/spec 接口用）。"""
    return {
        "grammar": "expr := or;  or := and (OR and)*;  and := not (AND not)*;  "
        "not := NOT not | atom;  atom := TERM | '*' | '(' expr ')'",
        "token_rule": r"TERM 为 \w+（unicode 字母/数字/下划线），统一小写；"
        "AND/OR/NOT 为保留关键字（大小写不敏感）",
        "universe_semantics": "`*` 是当前版本显式文档全集；NOT 仅对该有限全集求补，"
        "绝不补成无限整数集",
        "normalization_example": tokenize("Hello, 世界! Fast-Index") ,
        "error_categories": list(ALL_CATEGORIES),
    }
