"""守卫：求交/并/补必须由游标核心完成，不能用 SQL 集合查询代替。

需求：“输出执行统计而非用数据库查询代替核心求交”。本测试静态检查
app/index/posting.py 与 app/query/executor.py 中不出现 SQL 层集合运算，
且 executor 真正调用 posting 的 intersect/union/difference。
"""
from __future__ import annotations

import pathlib

import app.index.posting as posting_mod
import app.query.executor as executor_mod

ROOT = pathlib.Path(posting_mod.__file__).resolve().parents[2]


def _read(mod) -> str:
    return pathlib.Path(mod.__file__).read_text(encoding="utf-8").lower()


def test_posting_core_contains_no_sql_set_operations():
    src = _read(posting_mod)
    for forbidden in ("select ", "sqlite", "cursor.execute"):
        assert forbidden not in src, f"posting 核心出现 SQL 痕迹：{forbidden!r}"
    # 也不允许出现 SQL 集合关键字作为语句
    for kw in ("intersect ", " except ", "union all"):
        assert kw not in src, f"posting 核心出现 SQL 集合运算：{kw!r}"


def test_executor_calls_core_set_functions_not_sql():
    src = _read(executor_mod)
    assert "p.intersect(" in src
    assert "p.union(" in src
    assert "p.difference(" in src
    # 不应直接在执行层写 SQL
    assert "select " not in src
    assert "conn.execute" not in src
    assert ".executemany" not in src
    # 存储层可以读 posting 列表，但没有任何集合运算 SQL。
    # 用“关键字后接 SELECT/表名”的 SQL 形态判定，避免误伤 Python 的 except。
    import re

    storage_src = (ROOT / "app" / "storage" / "version_store.py").read_text(
        "utf-8"
    )
    sql_setops = re.compile(
        r"\b(INTERSECT|EXCEPT)\b\s+(SELECT|DISTINCT|ALL)?\s*SELECT"
        r"|\bUNION\s+ALL\b",
        re.IGNORECASE,
    )
    assert not sql_setops.search(storage_src), "存储层不应做 SQL 集合运算"


def test_executor_stats_come_from_cursors(seeded_store):
    """端到端：统计字段由游标累加，且与步骤明细自洽。"""
    from app.query.engine import QueryEngine

    eng = QueryEngine(seeded_store)
    out = eng.query("alpha AND beta AND gamma", version=2)
    assert out.ok
    # 全局统计 == 各步骤统计之和
    steps_total = {
        k: sum(s["stats"][k] for s in out.steps)
        for k in (
            "comparisons",
            "docs_examined",
            "blocks_skipped",
            "docs_skipped_in_blocks",
        )
    }
    for k, v in steps_total.items():
        assert out.stats[k] == v
    assert out.stats["comparisons"] > 0
