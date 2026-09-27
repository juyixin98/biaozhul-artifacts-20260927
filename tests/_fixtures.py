"""测试辅助：从 VersionStore 构造 oracle 需要的“版本 -> term 集合”视图。"""
from __future__ import annotations

import sqlite3

from tests._oracle import oracle_answer


def term_sets_for_version(store, version: int) -> dict[str, set[int]]:
    """直接读 postings（原始已索引集合）+ universe 快照，构造 oracle 输入。"""
    conn: sqlite3.Connection = store._conn  # 测试中可接受内部读取
    visible = {
        r[0]
        for r in conn.execute(
            "SELECT doc_id FROM universe_members WHERE version=?", (version,)
        )
    }
    term_sets: dict[str, set[int]] = {}
    for term, doc_id in conn.execute("SELECT term, doc_id FROM postings"):
        if doc_id in visible:
            term_sets.setdefault(term, set()).add(doc_id)
    return term_sets, visible


def expected(store, expression: str, version: int) -> set[int]:
    term_sets, universe = term_sets_for_version(store, version)
    return oracle_answer(expression, term_sets, universe)
