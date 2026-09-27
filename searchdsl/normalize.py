"""逻辑化简 / 规范化。

规则（保持语义，且幂等）：
1. 嵌套同名布尔节点拍平：And(And(a,b),c) -> And(a,b,c)，Or 同理；
2. 删除重复子句：a AND a -> a、a OR a -> a（以规范化键判重）；
3. 子节点按确定性键排序；
4. 双重否定消去：NOT(NOT(x)) -> x；
5. 单子句布尔节点脱壳：And(x) -> x；
6. 空查询 Empty 保持不变（空查询 = 匹配全部，绝不能化简成无结果）。

不做德摩根/分配律：那会改变树深与子句规模，与复杂度预算冲突。
不存在字段语义由 validate 模块在规范化*之前*拦截，本模块不静默丢弃字段。
"""

from __future__ import annotations

from typing import List

from . import ast_nodes as ast


def _key(node: ast.Node) -> str:
    """节点的确定性序列化键（排序、判重用）。"""
    import json
    return json.dumps(ast.to_dict(node), sort_keys=True, ensure_ascii=False)


def normalize(node: ast.Node) -> ast.Node:
    if isinstance(node, ast.Empty):
        return ast.Empty()

    if isinstance(node, (ast.Term, ast.Phrase)):
        return node  # 叶子不可变

    if isinstance(node, ast.Not):
        inner = normalize(node.child)
        if isinstance(inner, ast.Not):
            return inner.child  # NOT(NOT(x)) -> x
        return ast.Not(inner)

    kind = type(node)
    flat: List[ast.Node] = []
    for child in node.children:
        norm = normalize(child)
        if isinstance(norm, kind):
            flat.extend(norm.children)  # 同名拍平
        else:
            flat.append(norm)

    seen: set[str] = set()
    unique: List[ast.Node] = []
    for child in flat:
        k = _key(child)
        if k not in seen:
            seen.add(k)
            unique.append(child)
    unique.sort(key=_key)

    if len(unique) == 1:
        return unique[0]
    return kind(tuple(unique))


def is_idempotent(node: ast.Node) -> bool:
    """normalize(normalize(node)) == normalize(node) 的判定。"""
    once = normalize(node)
    twice = normalize(once)
    return _key(once) == _key(twice)
