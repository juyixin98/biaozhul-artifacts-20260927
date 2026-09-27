"""计划执行器：真正的求交/并/补都在这里完成（不在 SQL 中做集合运算）。

执行特性
--------
- AND：成对折叠。任一中间结果为空即**短路**，剩余子节点不再取数/执行，
  并在 stats 中报告 ``short_circuited=True`` 与未执行节点。
- OR：成对归并求并（不短路到“全集合”之外的任何提前返回，保证完整正确）；
  若中间结果已覆盖显式全集则提前结束（全集短路，仅在安全时发生）。
- NOT：``universe_difference(显式版本全集, 子结果)``——永远是有限补集。
- 每个节点返回的列表严格递增无重复；visible 根节点再与可见全集求交，
  保证删除文档不可见，且结果 ID 唯一。

统计
----
每个节点记录游标比较次数、逐文档检查数、整块跳过数（及跳过块内文档数），
聚合成全局执行统计。统计来自核心算法，而不是用数据库 COUNT 查询替代求交。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from ..index import posting as P
from ..text import spec
from . import planner as PL


@dataclass
class StepStats:
    comparisons: int = 0
    docs_examined: int = 0
    blocks_skipped: int = 0
    docs_skipped_in_blocks: int = 0
    next_calls: int = 0
    advance_calls: int = 0

    def add_op(self, op: P.OpStats) -> None:
        self.comparisons += op.comparisons
        self.docs_examined += op.doc_examined
        self.blocks_skipped += op.blocks_skipped
        self.docs_skipped_in_blocks += op.docs_skipped_in_blocks
        self.next_calls += op.next_calls
        self.advance_calls += op.advance_calls


@dataclass
class Step:
    """关键步骤：接口与日志据此解释“做了什么、在哪、省了多少”。"""

    node_id: str
    op: str  # term_load | universe_load | intersect | union | universe_difference | visible_filter
    label: str
    result_size: int
    stats: StepStats = field(default_factory=StepStats)
    short_circuited: bool = False
    skipped_children: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stats"] = asdict(self.stats)
        return d


@dataclass
class ExecResult:
    ids: list[int]
    steps: list[Step]
    stats: StepStats
    short_circuited: bool
    skipped_nodes: list[str]

    def total_blocks_skipped(self) -> int:
        return sum(s.stats.blocks_skipped for s in self.steps)


class Executor:
    def __init__(
        self,
        store,
        version: int,
        order: str = PL.ORDER_RARE_FIRST,
        block_size: int | None = None,
    ) -> None:
        self.store = store
        self.version = version
        if order not in PL.ALL_ORDERS:
            raise PL.QueryValidationError(f"未知执行顺序：{order}")
        self.order = order
        self.block_size = block_size if block_size is not None else store.block_size

    # -- 入口 ------------------------------------------------------------

    def execute(self, plan: PL.Plan) -> ExecResult:
        self.steps: list[Step] = []
        self.short_circuited = False
        self.skipped_nodes: list[str] = []
        ids_pl = self._eval(plan.root)
        total = StepStats()
        for s in self.steps:
            for f in (
                "comparisons",
                "docs_examined",
                "blocks_skipped",
                "docs_skipped_in_blocks",
                "next_calls",
                "advance_calls",
            ):
                setattr(total, f, getattr(total, f) + getattr(s.stats, f))
        ids = list(ids_pl.ids)
        # 最终不变量：结果严格递增且唯一
        if ids != sorted(set(ids)):
            raise RuntimeError("内部不变量被破坏：结果非严格递增或存在重复 ID")
        return ExecResult(
            ids=ids,
            steps=self.steps,
            stats=total,
            short_circuited=self.short_circuited,
            skipped_nodes=self.skipped_nodes,
        )

    # -- 递归执行 --------------------------------------------------------

    def _label(self, node: PL.PlanNode) -> str:
        if node.kind == "term":
            return f"term:{node.term}"
        if node.kind == "universe":
            return "*"
        if node.kind == "visible":
            return "visible_universe_filter"
        return f"{node.kind}@{node.node_id}"

    def _eval(self, node: PL.PlanNode) -> P.PostingList:
        if node.kind == "term":
            return self._eval_term(node)
        if node.kind == "universe":
            return self._eval_universe(node)
        if node.kind == "not":
            return self._eval_not(node)
        if node.kind == "and":
            return self._eval_and(node)
        if node.kind == "or":
            return self._eval_or(node)
        if node.kind == "visible":
            return self._eval_visible(node)
        raise PL.QueryValidationError(f"未知计划节点：{node.kind}")

    def _eval_term(self, node: PL.PlanNode) -> P.PostingList:
        # 直接取持久化 posting 列表（未做集合运算；仅有序读取）
        plist = self.store.posting(node.term)
        self.steps.append(
            Step(
                node_id=node.node_id,
                op="term_load",
                label=self._label(node),
                result_size=len(plist),
                detail=f"从持久块读取 term={node.term!r}，"
                f"块数={len(plist.blocks)}，块上界={list(plist.block_uppers)}",
            )
        )
        return plist

    def _eval_universe(self, node: PL.PlanNode) -> P.PostingList:
        plist = self.store.universe(self.version)
        self.steps.append(
            Step(
                node_id=node.node_id,
                op="universe_load",
                label=self._label(node),
                result_size=len(plist),
                detail=f"载入版本 {self.version} 的显式文档全集（大小={len(plist)}）",
            )
        )
        return plist

    def _eval_visible(self, node: PL.PlanNode) -> P.PostingList:
        child_result = self._eval(node.children[0])
        universe = self.store.universe(self.version)
        filtered, op = P.intersect(child_result, universe)
        st = StepStats()
        st.add_op(op)
        self.steps.append(
            Step(
                node_id=node.node_id,
                op="visible_filter",
                label=self._label(node),
                result_size=len(filtered),
                stats=st,
                detail=f"与版本 {self.version} 可见全集求交，"
                f"移除可能已删除文档；跳过块={op.blocks_skipped}",
            )
        )
        return filtered

    def _eval_not(self, node: PL.PlanNode) -> P.PostingList:
        child_result = self._eval(node.children[0])
        universe = self.store.universe(self.version)
        result, op = P.difference(universe, child_result)
        st = StepStats()
        st.add_op(op)
        self.steps.append(
            Step(
                node_id=node.node_id,
                op="universe_difference",
                label=self._label(node),
                result_size=len(result),
                stats=st,
                detail=(
                    f"显式全集({len(universe)}) - 子结果({len(child_result)})"
                    f" = {len(result)}；NOT 不补成无限集；跳过块={op.blocks_skipped}"
                ),
            )
        )
        return result

    def _ordered_children(self, node: PL.PlanNode) -> list[PL.PlanNode]:
        return PL.order_children(node.children, self.order, parent_kind=node.kind)

    def _eval_and(self, node: PL.PlanNode) -> P.PostingList:
        ordered = self._ordered_children(node)
        acc = self._eval(ordered[0])
        for idx in range(1, len(ordered)):
            child = ordered[idx]
            if len(acc) == 0:
                # 短路：AND 遇空即终，剩余子树完全不求值
                skipped = [c.node_id for c in ordered[idx:]]
                self.short_circuited = True
                self.skipped_nodes.extend(skipped)
                self.steps.append(
                    Step(
                        node_id=node.node_id,
                        op="intersect",
                        label=self._label(node),
                        result_size=0,
                        short_circuited=True,
                        skipped_children=skipped,
                        detail="AND 中间结果为空 → 短路，剩余子节点未执行",
                    )
                )
                return P.PostingList.empty(self.block_size)
            cand = self._eval(child)
            acc, op = P.intersect(acc, cand)
            st = StepStats()
            st.add_op(op)
            self.steps.append(
                Step(
                    node_id=node.node_id,
                    op="intersect",
                    label=f"{self._label(node)} ∩ {self._label(child)}",
                    result_size=len(acc),
                    stats=st,
                    detail=(
                        f"与 {self._label(child)} 求交；"
                        f"比较={op.comparisons}，整块跳过={op.blocks_skipped}，"
                        f"跳过块内文档={op.docs_skipped_in_blocks}"
                    ),
                )
            )
        return acc

    def _eval_or(self, node: PL.PlanNode) -> P.PostingList:
        ordered = self._ordered_children(node)
        universe = self.store.universe(self.version)
        universe_ids = set(universe.ids)
        acc = self._eval(ordered[0])
        for idx in range(1, len(ordered)):
            child = ordered[idx]
            # 仅当 acc **真正覆盖**显式全集时才可安全短路（集合相等）；
            # 只比较大小是不够的：长度相同也可能只是错位的子集。
            if universe_ids and set(acc.ids) >= universe_ids:
                # 已覆盖显式全集 → 并集结果不可能再变化（全集短路）
                skipped = [c.node_id for c in ordered[idx:]]
                self.short_circuited = True
                self.skipped_nodes.extend(skipped)
                self.steps.append(
                    Step(
                        node_id=node.node_id,
                        op="union",
                        label=self._label(node),
                        result_size=len(acc),
                        short_circuited=True,
                        skipped_children=skipped,
                        detail="OR 中间结果已覆盖显式全集 → 短路",
                    )
                )
                return acc
            cand = self._eval(child)
            acc, op = P.union(acc, cand)
            st = StepStats()
            st.add_op(op)
            self.steps.append(
                Step(
                    node_id=node.node_id,
                    op="union",
                    label=f"{self._label(node)} ∪ {self._label(child)}",
                    result_size=len(acc),
                    stats=st,
                    detail=(
                        f"与 {self._label(child)} 求并（相遇 ID 只收一次）；"
                        f"比较={op.comparisons}，整块跳过={op.blocks_skipped}"
                    ),
                )
            )
        return acc
