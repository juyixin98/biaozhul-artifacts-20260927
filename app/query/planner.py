"""逻辑计划：把文本规范 AST 绑定到存储，并在外层包装“可见全集”。

计划节点种类：term / universe / not / and / or / visible。
- visible 节点恒位于计划根部：结果 ∩ 当前版本显式可见全集。
  删除文档因此同步影响所有查询，而无需重写任何 posting 块。
- universe 节点（用户写的 `*`）也解析到同一个版本化全集。
- 未知 term 不报错：绑定为长度 0 的 term 列表，并在 warnings 中提示
  （“不确定结论”单列），由执行层正常参与集合代数。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..index.posting import PostingList
from ..text import spec

ORDER_RARE_FIRST = "rare_first"  # 按估计长度升序（默认/auto）
ORDER_TEXTUAL = "textual"  # 按表达式从左到右
ORDER_REVERSE = "reverse"  # 按表达式从右到左
ALL_ORDERS = (ORDER_RARE_FIRST, ORDER_TEXTUAL, ORDER_REVERSE)


class QueryValidationError(ValueError):
    """查询计划阶段的失败（类别 validation_error）。"""


@dataclass(frozen=True)
class PlanNode:
    kind: str  # term | universe | not | and | or | visible
    ast: spec.Node
    children: tuple["PlanNode", ...] = ()
    term: str | None = None
    estimated_size: int = 0
    node_id: str = ""


@dataclass
class Plan:
    root: PlanNode
    version: int
    expression: str
    warnings: list[str] = field(default_factory=list)
    referenced_terms: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, store, version: int, expression: str) -> None:
        self.store = store
        self.version = version
        self.expression = expression
        self.warnings: list[str] = []
        self.known_terms: set[str] = set(self.store.list_terms(limit=1_000_000))
        self._universe_size = len(self.store.universe(version))
        self._counter = 0

    def _nid(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _bind(self, node: spec.Node) -> PlanNode:
        if isinstance(node, spec.Term):
            plist = self.store.posting(node.value)
            exists = node.value in self.known_terms
            if not exists:
                self.warnings.append(
                    f"term {node.value!r}（字符位置 {node.span[0]}..{node.span[1]}）"
                    f"在索引中不存在，按空列表处理"
                )
            return PlanNode(
                kind="term",
                ast=node,
                term=node.value,
                estimated_size=len(plist),
                node_id=self._nid("t"),
            )
        if isinstance(node, spec.Universe):
            return PlanNode(
                kind="universe",
                ast=node,
                estimated_size=self._universe_size,
                node_id=self._nid("u"),
            )
        if isinstance(node, spec.Not):
            child = self._bind(node.child)
            # NOT 结果上界就是显式全集大小
            return PlanNode(
                kind="not",
                ast=node,
                children=(child,),
                estimated_size=self._universe_size,
                node_id=self._nid("n"),
            )
        if isinstance(node, spec.And):
            children = tuple(self._bind(c) for c in node.children)
            return PlanNode(
                kind="and",
                ast=node,
                children=children,
                estimated_size=min(c.estimated_size for c in children),
                node_id=self._nid("a"),
            )
        if isinstance(node, spec.Or):
            children = tuple(self._bind(c) for c in node.children)
            return PlanNode(
                kind="or",
                ast=node,
                children=children,
                estimated_size=min(
                    self._universe_size, sum(c.estimated_size for c in children)
                ),
                node_id=self._nid("o"),
            )
        raise QueryValidationError(f"未知 AST 节点：{type(node).__name__}")

    def build(self) -> Plan:
        inner = self._bind(spec.parse_query(self.expression))
        visible_wrapper = PlanNode(
            kind="visible",
            ast=inner.ast,
            children=(inner,),
            estimated_size=self._universe_size,
            node_id="vis0",
        )
        terms: list[str] = []
        for t in spec.terms_in(spec.parse_query(self.expression)):
            if t not in terms:
                terms.append(t)
        return Plan(
            root=visible_wrapper,
            version=self._version,
            expression=self.expression,
            warnings=self.warnings,
            referenced_terms=terms,
        )

    @property
    def _version(self) -> int:
        return self.version


def order_children(
    children: tuple[PlanNode, ...], order: str, parent_kind: str = "and"
) -> list[PlanNode]:
    """AND/OR 子节点的执行顺序策略。

    不同顺序必须产生**完全相同的结果集合**（集合代数结合/交换律），
    但跳过块数、检查文档数可以不同——这正是 explain 要展示的。

    rare_first（auto）：
    - AND：按估计长度升序，最稀疏列表驱动，最快产生空集触发短路；
    - OR ：同样短列表优先，但 `*`（全集）子节点排最前——OR 一旦遇到全集
      即可安全短路（吸收律 A∨U=U），无需扫描任何其他子树。
    """
    if order == ORDER_TEXTUAL:
        return list(children)
    if order == ORDER_REVERSE:
        return list(reversed(children))
    if order == ORDER_RARE_FIRST:
        if parent_kind == "or":
            return sorted(
                children,
                # universe 节点优先（吸收短路），其余按估计长度升序
                key=lambda c: (0 if c.kind == "universe" else 1,
                               c.estimated_size, c.node_id),
            )
        return sorted(children, key=lambda c: (c.estimated_size, c.node_id))
    raise QueryValidationError(f"未知执行顺序：{order}")
