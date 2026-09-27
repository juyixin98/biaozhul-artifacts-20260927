"""规范查询树（AST）节点定义与序列化。

节点均为不可变值对象；canonical() 给出确定性的字典表示，
是规范化幂等判定与版本存储内容寻址的基础。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Optional, Tuple, Union


@dataclass(frozen=True)
class Empty:
    """空查询：匹配全部文档。规范化不得把它改成别的节点。"""


@dataclass(frozen=True)
class Term:
    value: str
    field: Optional[str] = None  # None 表示在默认字段集合上检索
    # 仅用于诊断定位，不参与结构相等/哈希（规范化判重只看语义）
    pos: Optional[int] = dc_field(default=None, compare=False)


@dataclass(frozen=True)
class Phrase:
    terms: Tuple[str, ...]       # 短语按 [a-z0-9]+ 切词（小写化）后的词序列
    field: Optional[str] = None
    pos: Optional[int] = dc_field(default=None, compare=False)


@dataclass(frozen=True)
class Not:
    child: "Node"


@dataclass(frozen=True)
class And:
    children: Tuple["Node", ...]


@dataclass(frozen=True)
class Or:
    children: Tuple["Node", ...]


Node = Union[Empty, Term, Phrase, Not, And, Or]


def to_dict(node: Node) -> dict:
    """把节点转成可 JSON 序列化的字典（保持字段名稳定）。"""
    if isinstance(node, Empty):
        return {"type": "empty"}
    if isinstance(node, Term):
        return {"type": "term", "field": node.field, "value": node.value}
    if isinstance(node, Phrase):
        return {"type": "phrase", "field": node.field, "terms": list(node.terms)}
    if isinstance(node, Not):
        return {"type": "not", "child": to_dict(node.child)}
    if isinstance(node, And):
        return {"type": "and", "children": [to_dict(c) for c in node.children]}
    if isinstance(node, Or):
        return {"type": "or", "children": [to_dict(c) for c in node.children]}
    raise TypeError(f"unknown node: {node!r}")


def canonical(node: Node) -> dict:
    """canonical 即 to_dict：And/Or 的子节点顺序由 normalize 负责确定。"""
    return to_dict(node)
