"""查询执行引擎。

职责
----
* 解析查询文本 -> AST（app.query.spec）
* 针对**某个显式版本**的存储加载 posting（BlockedPostingList）
* 递归求值 AST：AND 顺序短路、OR 全集覆盖短路、NOT 相对显式版本全集
* 汇总执行统计（跳过块数等），并生成可解释轨迹
* 失败统一携带 ErrorCategory；“不确定结论”（如未知词项被按空集处理）
  与确定失败分区记录

NOT 语义（关键约束）
--------------------
``NOT X`` 定义为 ``U(version) ＼ eval(X)``，其中 ``U(version)`` 是该版本
**可见**文档的有限全集，由版本存储显式给出；不会向任何无限整数集扩展。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..postings.blocked_list import BlockedPostingList, OpStats
from ..postings.operators import difference, intersect, union
from ..storage.version_store import VersionStore
from ..diagnostics.logging_setup import get_logger
from .spec import (
    And,
    ErrorCategory,
    Node,
    Not,
    Or,
    QueryError,
    Term,
    collect_terms,
    parse_query,
)


class UnknownTermError(QueryError):
    category = ErrorCategory.UNKNOWN_TERM

    def __init__(self, terms: Sequence[str]):
        self.terms = list(terms)
        super().__init__(f"版本中不存在的词项：{self.terms}")


@dataclass
class NodeTrace:
    """单个 AST 节点的执行轨迹。"""

    label: str
    pos: int
    short_circuited: bool = False
    visited_children: int = 0
    stats: Dict[str, int] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    children: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "pos": self.pos,
            "short_circuited": self.short_circuited,
            "visited_children": self.visited_children,
            "stats": self.stats,
            "detail": self.detail,
            "children": self.children,
        }


@dataclass
class QueryResult:
    request_id: str
    query: str
    version: int
    doc_ids: List[int]
    stats: Dict[str, int]
    trace: Dict[str, Any]
    uncertainties: List[Dict[str, Any]]


class Engine:
    """AST 求值器。operand_order 用于验证执行顺序无关性（仅影响统计）。"""

    def __init__(
        self,
        store: VersionStore,
        block_size: int = 8,
        unknown_terms_empty: bool = False,
        logger: Optional[logging.Logger] = None,
    ):
        self.store = store
        self.block_size = block_size
        # True 时未知词项按空 posting 处理（同时记录为“不确定结论”）；
        # False（默认）时分类为 unknown_term 失败。
        self.unknown_terms_empty = unknown_terms_empty
        self.log = logger or get_logger()

    # ---------------- 对外入口 ----------------

    def execute(
        self,
        query: str,
        version: Optional[int] = None,
        operand_order: str = "left_to_right",
        request_id: str = "-",
        trace_sink: Optional[Any] = None,
    ) -> QueryResult:
        """执行查询；version=None 表示最新版本。

        operand_order:
          - "left_to_right"：按文本顺序求 AND
          - "right_to_left"：反转 AND 子节点顺序（结果必须相同，统计可不同）
        """
        from ..diagnostics.trace import Trace

        trace = trace_sink if trace_sink is not None else Trace(request_id)
        trace.set_summary(query=query, requested_version=version, operand_order=operand_order)
        trace.add_step("parse", detail={"text": query})

        ast = parse_query(query)  # parse_error 直接抛出（由 API 层分类）
        version_id = self.store.resolve_version(version)
        self.store.require_version(version_id)
        trace.set_summary(resolved_version=version_id)

        terms = collect_terms(ast)
        posting_map = self.store.bulk_postings(version_id, terms)
        known = set(self.store.known_terms(version_id))
        unknown = [t for t in terms if t not in known]
        if unknown:
            if not self.unknown_terms_empty:
                trace.add_failure(
                    ErrorCategory.UNKNOWN_TERM.value,
                    "查询引用了该版本中不存在的词项",
                    unknown_terms=unknown,
                    version=version_id,
                )
                trace.set_summary(status="failed", result_count=0)
                self.log.warning(
                    "unknown terms",
                    extra={
                        "event": "unknown_terms",
                        "request_id": request_id,
                        "version": version_id,
                        "category": ErrorCategory.UNKNOWN_TERM.value,
                        "detail": {"unknown_terms": unknown},
                    },
                )
                raise UnknownTermError(unknown)
            for t in unknown:
                trace.add_uncertainty(
                    "未知词项按空 posting 处理（unknown_terms_empty=true）",
                    term=t,
                    version=version_id,
                )

        universe_ids = self.store.universe(version_id)
        universe = BlockedPostingList.from_ids("<universe>", universe_ids, self.block_size)
        trace.add_step(
            "load_universe",
            version=version_id,
            universe_size=universe.length,
        )
        trace.set_summary(universe_size=universe.length)

        ctx = _EvalContext(
            store=self.store,
            version=version_id,
            block_size=self.block_size,
            posting_map=posting_map,
            universe=universe,
            operand_order=operand_order,
            request_id=request_id,
            trace=trace,
        )
        result, total = self._eval(ast, ctx)
        total.results_emitted = result.length  # 根统计=最终结果量（非跨节点累加）

        # 出口唯一性校验：核心合并已保证唯一，这里再断言一次。
        if len(result.doc_ids) != len(set(result.doc_ids)):
            raise AssertionError("结果 ID 不唯一，违反核心不变量")

        trace.set_summary(
            status="ok",
            result_count=result.length,
            stats=total.as_dict(),
        )
        self.log.info(
            "query executed",
            extra={
                "event": "query_executed",
                "request_id": request_id,
                "query": query,
                "version": version_id,
                "result_count": result.length,
                "stats": total.as_dict(),
            },
        )
        return QueryResult(
            request_id=request_id,
            query=query,
            version=version_id,
            doc_ids=list(result.doc_ids),
            stats=total.as_dict(),
            trace=trace.as_dict(),
            uncertainties=trace.uncertainties,
        )

    # ---------------- 递归求值 ----------------

    def _eval(self, node: Node, ctx: "_EvalContext") -> Tuple[BlockedPostingList, OpStats]:
        if isinstance(node, Term):
            return self._eval_term(node, ctx)
        if isinstance(node, And):
            return self._eval_and(node, ctx)
        if isinstance(node, Or):
            return self._eval_or(node, ctx)
        if isinstance(node, Not):
            return self._eval_not(node, ctx)
        raise QueryError(f"未知 AST 节点类型：{type(node).__name__}")

    def _eval_term(self, node: Term, ctx: "_EvalContext") -> Tuple[BlockedPostingList, OpStats]:
        ids = ctx.posting_map.get(node.term, [])
        plist = BlockedPostingList.from_ids(
            f"term:{node.term}", ids, self.block_size
        )
        stats = OpStats(results_emitted=plist.length)
        nt = NodeTrace(
            label=f"TERM {node.term}",
            pos=node.pos,
            stats=stats.as_dict(),
            detail={"posting_size": plist.length},
        )
        ctx.trace.add_step("eval_term", term=node.term, pos=node.pos, size=plist.length)
        ctx.trace_node(node, nt)
        return plist, stats

    def _ordered(self, node: And | Or, ctx: "_EvalContext") -> List[Node]:
        children = list(node.children)
        if ctx.operand_order == "right_to_left":
            children.reverse()
        elif ctx.operand_order != "left_to_right":
            raise QueryError(
                f"未知 operand_order={ctx.operand_order!r}，"
                "可选 left_to_right / right_to_left"
            )
        return children

    def _eval_and(self, node: And, ctx: "_EvalContext") -> Tuple[BlockedPostingList, OpStats]:
        nt = NodeTrace(label="AND", pos=node.pos)
        children = self._ordered(node, ctx)

        first, acc_stats = self._eval(children[0], ctx)
        acc = first
        nt.visited_children = 1

        stage_stats: List[Dict[str, Any]] = []
        short_circuit_at: Optional[int] = None

        # AND 顺序短路：任一中间结果为空即停止，剩余子节点不求值。
        for index in range(1, len(children)):
            if acc.length == 0:
                short_circuit_at = index
                break
            child_plist, child_stats = self._eval(children[index], ctx)
            nt.visited_children += 1
            merged, op_stats = intersect(acc, child_plist)
            stage_stats.append(
                {
                    "stage": index,
                    "intersect_with": _describe(children[index]),
                    "left_size": acc.length,
                    "right_size": child_plist.length,
                    "result_size": merged.length,
                    "stats": op_stats.as_dict(),
                }
            )
            acc = merged
            acc_stats = acc_stats.merge(child_stats).merge(op_stats)

        nt.short_circuited = short_circuit_at is not None
        acc_stats.results_emitted = acc.length  # 本 AND 节点最终输出量
        nt.stats = acc_stats.as_dict()
        nt.detail = {
            "operand_order": ctx.operand_order,
            "total_children": len(children),
            "short_circuit_at": short_circuit_at,
            "stages": stage_stats,
            "result_size": acc.length,
        }
        ctx.trace.add_step(
            "eval_and",
            pos=node.pos,
            visited=nt.visited_children,
            total=len(children),
            short_circuited=nt.short_circuited,
            blocks_skipped=acc_stats.blocks_skipped,
        )
        ctx.trace_node(node, nt)
        return acc, acc_stats

    def _eval_or(self, node: Or, ctx: "_EvalContext") -> Tuple[BlockedPostingList, OpStats]:
        nt = NodeTrace(label="OR", pos=node.pos)
        children = self._ordered(node, ctx)

        acc, acc_stats = self._eval(children[0], ctx)
        nt.visited_children = 1
        stage_stats: List[Dict[str, Any]] = []
        short_circuit_at: Optional[int] = None

        # OR 全集覆盖短路：并集已覆盖整个（非空）全集即停止。
        for index in range(1, len(children)):
            if acc.length == ctx.universe.length and ctx.universe.length > 0:
                short_circuit_at = index
                break
            child_plist, child_stats = self._eval(children[index], ctx)
            nt.visited_children += 1
            merged, op_stats = union(acc, child_plist)
            stage_stats.append(
                {
                    "stage": index,
                    "union_with": _describe(children[index]),
                    "left_size": acc.length,
                    "right_size": child_plist.length,
                    "result_size": merged.length,
                    "stats": op_stats.as_dict(),
                }
            )
            acc = merged
            acc_stats = acc_stats.merge(child_stats).merge(op_stats)

        nt.short_circuited = short_circuit_at is not None
        acc_stats.results_emitted = acc.length  # 本 OR 节点最终输出量
        nt.stats = acc_stats.as_dict()
        nt.detail = {
            "operand_order": ctx.operand_order,
            "total_children": len(children),
            "short_circuit_at": short_circuit_at,
            "stages": stage_stats,
            "result_size": acc.length,
            "universe_size": ctx.universe.length,
        }
        ctx.trace.add_step(
            "eval_or",
            pos=node.pos,
            visited=nt.visited_children,
            total=len(children),
            short_circuited=nt.short_circuited,
        )
        ctx.trace_node(node, nt)
        return acc, acc_stats

    def _eval_not(self, node: Not, ctx: "_EvalContext") -> Tuple[BlockedPostingList, OpStats]:
        nt = NodeTrace(label="NOT", pos=node.pos)
        child_plist, child_stats = self._eval(node.child, ctx)
        # NOT = U(version) ＼ child。U 是有限、显式版本化的可见全集。
        result, op_stats = difference(ctx.universe, child_plist)
        total = child_stats.merge(op_stats)
        total.results_emitted = result.length  # 本 NOT 节点最终输出量
        nt.visited_children = 1
        nt.stats = total.as_dict()
        nt.detail = {
            "universe_size": ctx.universe.length,
            "excluded_size": child_plist.length,
            "result_size": result.length,
            "universe_version": ctx.version,
            "note": "NOT 相对于该版本的可见文档全集求补（有限集）",
        }
        ctx.trace.add_step(
            "eval_not",
            pos=node.pos,
            version=ctx.version,
            universe_size=ctx.universe.length,
            excluded_size=child_plist.length,
            blocks_skipped=op_stats.blocks_skipped,
        )
        ctx.trace_node(node, nt)
        return result, total


@dataclass
class _EvalContext:
    store: VersionStore
    version: int
    block_size: int
    posting_map: Dict[str, List[int]]
    universe: BlockedPostingList
    operand_order: str
    request_id: str
    trace: Any

    def trace_node(self, node: Node, nt: NodeTrace) -> None:
        # 轨迹以扁平步骤 + 节点详情两种形态留存，便于诊断检索。
        self.trace.add_step("node", **nt.as_dict())


def _describe(node: Node) -> str:
    if isinstance(node, Term):
        return f"TERM {node.term}"
    return type(node).__name__.upper()
