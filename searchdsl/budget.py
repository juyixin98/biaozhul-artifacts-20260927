"""执行前复杂度预算检查。

- depth：树深（叶子=1，Not=1+child，And/Or=1+max(children)）。
  预算在规范化之前检查，因此靠深括号/深嵌套绕过限制是无效的。
- clauses：叶子子句数（Term/Phrase），限制子句膨胀。
- phrase_terms：单个短语的词数。
- term_length：裸词长度。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import DslError, ErrorCategory


@dataclass(frozen=True)
class Budget:
    max_depth: int
    max_clauses: int
    max_phrase_terms: int
    max_term_length: int


@dataclass(frozen=True)
class BudgetUsage:
    depth: int
    clauses: int


def measure(node: ast.Node) -> BudgetUsage:
    if isinstance(node, ast.Empty):
        return BudgetUsage(depth=0, clauses=0)
    if isinstance(node, (ast.Term, ast.Phrase)):
        return BudgetUsage(depth=1, clauses=1)
    if isinstance(node, ast.Not):
        sub = measure(node.child)
        return BudgetUsage(depth=sub.depth + 1, clauses=sub.clauses)
    depth = 1 + max(measure(c).depth for c in node.children)
    clauses = sum(measure(c).clauses for c in node.children)
    return BudgetUsage(depth=depth, clauses=clauses)


def enforce(node: ast.Node, budget: Budget) -> BudgetUsage:
    """超限即抛 BUDGET_EXCEEDED；空查询（0 深度/0 子句）总是通过。"""
    usage = measure(node)
    if isinstance(node, ast.Empty):
        return usage
    if usage.depth > budget.max_depth:
        raise DslError(
            ErrorCategory.BUDGET,
            f"查询嵌套深度 {usage.depth} 超过预算 {budget.max_depth}（最大允许 {budget.max_depth} 层）",
        )
    if usage.clauses > budget.max_clauses:
        raise DslError(
            ErrorCategory.BUDGET,
            f"子句数量 {usage.clauses} 超过预算 {budget.max_clauses}（最多 {budget.max_clauses} 个）",
        )
    _check_leaf_limits(node, budget)
    return usage


def _check_leaf_limits(node: ast.Node, budget: Budget) -> None:
    if isinstance(node, ast.Term):
        if len(node.value) > budget.max_term_length:
            raise DslError(
                ErrorCategory.BUDGET,
                f"词长度 {len(node.value)} 超过上限 {budget.max_term_length}",
            )
    elif isinstance(node, ast.Phrase):
        if len(node.terms) > budget.max_phrase_terms:
            raise DslError(
                ErrorCategory.BUDGET,
                f"短语词数 {len(node.terms)} 超过上限 {budget.max_phrase_terms}",
            )
    elif isinstance(node, ast.Not):
        _check_leaf_limits(node.child, budget)
    elif isinstance(node, (ast.And, ast.Or)):
        for child in node.children:
            _check_leaf_limits(child, budget)
