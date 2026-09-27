"""独立参考实现（oracle）：纯 Python 集合代数 + 自己的解析器。

重要约束：本文件**不得**导入 app 包中的任何模块——参考答案不能由被测核心
自身生成。tests/unit/test_oracle_independence.py 会静态强制这一点。

oracle 直接在“文档 -> term 集合”的夹具上用 Python set 计算期望结果，
与持久化存储、跳跃块、游标完全无关。
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\s*(\(|\)|\*|[\w]+)", re.UNICODE)


class OracleError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 自带的最小表达式解析器（与 app.text.spec 相互独立）
# ---------------------------------------------------------------------------


def _tokens(text: str):
    pos = 0
    out = []
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            if text[pos].isspace():
                pos += 1
                continue
            raise OracleError(f"非法字符 @ {pos}: {text[pos]!r}")
        out.append((m.group(1), m.start(1)))
        pos = m.end()
    return out


def parse_expr(text: str):
    toks = _tokens(text)
    if not toks:
        raise OracleError("空表达式")
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else (None, None)

    def advance():
        t = peek()
        pos[0] += 1
        return t

    def p_atom():
        t, span = advance()
        if t is None:
            raise OracleError("意外结束")
        if t == "(":
            node = p_or()
            c, _ = advance()
            if c != ")":
                raise OracleError("缺少右括号")
            return node
        if t == ")":
            raise OracleError("多余右括号")
        low = t.lower()
        if low in ("and", "or", "not"):
            raise OracleError(f"关键字位置错误: {t}")
        if t == "*":
            return ("*",)
        return ("term", low)

    def p_not():
        t, _ = peek()
        if t is not None and t.lower() == "not":
            advance()
            return ("not", p_not())
        return p_atom()

    def p_and():
        node = p_not()
        while True:
            t, _ = peek()
            if t is not None and t.lower() == "and":
                advance()
                node = ("and", node, p_not())
            else:
                return node

    def p_or():
        node = p_and()
        while True:
            t, _ = peek()
            if t is not None and t.lower() == "or":
                advance()
                node = ("or", node, p_and())
            else:
                return node

    tree = p_or()
    if peek()[0] is not None:
        raise OracleError(f"多余词素: {peek()}")
    return tree


def eval_set(tree, term_sets: dict[str, set[int]], universe: set[int]) -> set[int]:
    """集合代数求值。term 缺失按空集；NOT 永远相对显式 universe 求补。"""
    tag = tree[0]
    if tag == "term":
        return set(term_sets.get(tree[1], set()))
    if tag == "*":
        return set(universe)
    if tag == "not":
        return set(universe) - eval_set(tree[1], term_sets, universe)
    if tag == "and":
        return eval_set(tree[1], term_sets, universe) & eval_set(
            tree[2], term_sets, universe
        )
    if tag == "or":
        return eval_set(tree[1], term_sets, universe) | eval_set(
            tree[2], term_sets, universe
        )
    raise OracleError(f"未知节点 {tag}")


def oracle_answer(
    expression: str, term_sets: dict[str, set[int]], universe: set[int]
) -> set[int]:
    return eval_set(parse_expr(expression), term_sets, universe)
