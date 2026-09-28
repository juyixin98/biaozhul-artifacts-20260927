"""血缘/LCA 内核单元测试。"""
from __future__ import annotations

import pytest

from merge3.kernel import lineage
from merge3.errors import NotFoundError, ValidationError


def test_linear_history_lca():
    # base -> c1 -> c2(dev)，main 停在 c1
    pm = {"base": [], "c1": ["base"], "c2": ["c1"], "main0": ["base"]}
    assert lineage.find_lca("c2", "main0", pm) == "base"
    assert lineage.find_lca("c2", "c1", pm) == "c1"
    assert lineage.is_ancestor("base", "c2", pm)
    assert lineage.is_ancestor("c1", "c1", pm)
    assert not lineage.is_ancestor("c2", "c1", pm)


def test_merge_diamond_lca_is_merge_base():
    # base 分出 dev/main，各有提交，m 是有两个父的合并提交
    pm = {
        "base": [],
        "d1": ["base"], "d2": ["d1"],
        "m1": ["base"], "m2": ["m1"],
        "merge": ["d2", "m2"],
    }
    assert lineage.find_lca("d2", "m2", pm) == "base"
    # 从新开发头（merge 之后再提交）与主头找祖先，应穿过两条父边
    pm["d3"] = ["merge"]
    assert lineage.find_lca("d3", "m2", pm) == "m2"


def test_missing_snapshot():
    with pytest.raises(NotFoundError):
        lineage.find_lca("x", "y", {"x": []})


def test_multiple_lca_rejected():
    # 交织历史：两个互不可达的共同祖先 d0/m0
    pm = {
        "root": [],
        "d0": ["root"], "m0": ["root"],
        "x": ["d0", "m0"], "y": ["m0", "d0"],
    }
    with pytest.raises(ValidationError, match="多个最低共同祖先"):
        lineage.find_lca("x", "y", pm)
