"""查询编排测试：阈值边缘、排序稳定、上限、不确定结论单列。"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.costs import cost_profile_from_dict
from app.index import LexiconIndex
from app.query import QueryRejected, correct

UNIT = cost_profile_from_dict({
    "insert": 1.0, "delete": 1.0, "substitute": 1.0, "transpose": 1.0,
    "substitute_table": {},
})


def _settings(**over):
    base = dict(
        db_path=":memory:", seed_path="", default_threshold=2.0,
        uncertainty_margin=0.5, max_query_length=8,
        max_candidates_evaluated=100, max_results=10,
    )
    base.update(over)
    return Settings(**base)


def _index(words):
    return LexiconIndex("v-test", [(w, 100 - i) for i, w in enumerate(sorted(words))])


def test_exact_match_and_threshold_edge():
    index = _index(["abc", "abd", "ab", "xyz"])
    out = correct("abc", profile=UNIT, index=index, threshold=1.0,
                  max_results=None, max_query_length=8,
                  max_candidates_evaluated=100, default_max_results=10,
                  uncertainty_margin=0.5)
    words = [c["word"] for c in out["candidates"]]
    assert words[0] == "abc"
    assert out["exact_match"] is True
    # 距离恰好 1 的候选在阈值 1.0 边缘，必须保留（<= 而非 <）
    assert "abd" in words and "ab" in words
    assert "xyz" not in words


def test_threshold_boundary_off_by_half():
    index = _index(["ab"])
    out = correct("abc", profile=UNIT, index=index, threshold=1.0,
                  max_results=None, max_query_length=8,
                  max_candidates_evaluated=100, default_max_results=10,
                  uncertainty_margin=0.5)
    assert [c["word"] for c in out["candidates"]] == ["ab"]
    # 距离 1.0 落在 [threshold-margin, threshold] -> 进入 uncertain
    assert out["uncertain"] and out["uncertain"][0]["word"] == "ab"

    out2 = correct("abc", profile=UNIT, index=index, threshold=0.9,
                   max_results=None, max_query_length=8,
                   max_candidates_evaluated=100, default_max_results=10,
                   uncertainty_margin=0.5)
    assert out2["candidates"] == []


def test_stable_ordering_tie_distance_then_word():
    index = _index(["abc", "xbc", "axc", "abx", "ab"])
    out = correct("abc", profile=UNIT, index=index, threshold=1.0,
                  max_results=None, max_query_length=8,
                  max_candidates_evaluated=100, default_max_results=10,
                  uncertainty_margin=0.0)
    words = [c["word"] for c in out["candidates"]]
    assert words == ["abc", "ab", "abx", "axc", "xbc"]


def test_too_long_rejected_with_code():
    index = _index(["abc"])
    with pytest.raises(QueryRejected) as exc:
        correct("abcdefghij", profile=UNIT, index=index, threshold=1.0,
                max_results=None, max_query_length=8,
                max_candidates_evaluated=100, default_max_results=10,
                uncertainty_margin=0.5)
    assert exc.value.code == "query_too_long"


def test_empty_rejected_with_code():
    index = _index(["abc"])
    with pytest.raises(QueryRejected) as exc:
        correct("   ", profile=UNIT, index=index, threshold=1.0,
                max_results=None, max_query_length=8,
                max_candidates_evaluated=100, default_max_results=10,
                uncertainty_margin=0.5)
    assert exc.value.code == "empty_query"


def test_candidate_cap_marks_uncertainty_note():
    words = [f"w{i:03d}" for i in range(30)] + ["abc"]
    index = _index(words)
    out = correct("abc", profile=UNIT, index=index, threshold=5.0,
                  max_results=None, max_query_length=8,
                  max_candidates_evaluated=10, default_max_results=5,
                  uncertainty_margin=0.5)
    stage_cap = next(s for s in out["diagnostics"]["stages"] if s["name"] == "cap")
    assert stage_cap["counts"]["truncated"] is True
    assert out["diagnostics"]["notes"], "达到上限必须给出不确定说明"


def test_returned_path_replayable():
    index = _index(["spelling"])
    out = correct("speling", profile=UNIT, index=index, threshold=2.0,
                  max_results=None, max_query_length=8,
                  max_candidates_evaluated=100, default_max_results=10,
                  uncertainty_margin=0.5)
    cand = out["candidates"][0]
    assert cand["word"] == "spelling"
    assert cand["distance"] == pytest.approx(1.0)
    # 编辑路径包含一次插入且顺序在拼写缺口处
    types = [m["type"] for m in cand["edit_path"]]
    assert types.count("insert") == 1


def test_transposition_candidate_distance_one():
    index = _index(["hello"])
    out = correct("hlelo", profile=UNIT, index=index, threshold=1.0,
                  max_results=None, max_query_length=8,
                  max_candidates_evaluated=100, default_max_results=10,
                  uncertainty_margin=0.5)
    assert out["candidates"][0]["word"] == "hello"
    assert out["candidates"][0]["distance"] == pytest.approx(1.0)
    assert [m["type"] for m in out["candidates"][0]["edit_path"]] == ["swap"]


def test_diagnostics_carry_stage_counts():
    index = _index(["abc", "xyz"])
    out = correct("abc", profile=UNIT, index=index, threshold=1.0,
                  max_results=None, max_query_length=8,
                  max_candidates_evaluated=100, default_max_results=10,
                  uncertainty_margin=0.5)
    names = [s["name"] for s in out["diagnostics"]["stages"]]
    assert names == ["normalize", "validate", "generate", "prune", "cap",
                     "evaluate", "rank"]
    assert out["version_id"] == "v-test"
