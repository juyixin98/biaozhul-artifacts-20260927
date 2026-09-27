"""Hand-computed expected result sets on the shipped 12-document fixture.

These sets were derived by reading fixtures/corpus.jsonl directly, not by
running the implementation. They pin end-to-end boolean semantics
(including unfielded defaults, phrases, ranges, NOT universe semantics,
missing fields and multi-valued keywords).
"""

from __future__ import annotations

from searchdsl.parser import parse
from searchdsl.normalize import normalize
from searchdsl.executor import execute


def ids(query, engine) -> set[str]:
    tree = normalize(parse(query))
    return set(execute(tree, engine.store, engine.schema).docs)


def test_unfielded_term_uses_title_and_body_only(engine):
    # d09 body ends "...the quick brown fox again"; d08 body contains the
    # Latin token "fox". notes (e.g. d03 "field report") is NOT a default
    # field and must not contribute.
    assert ids("fox", engine) == {"d01", "d03", "d07", "d08", "d09", "d12"}


def test_implicit_and_intersection(engine):
    # quick AND fox across default fields.
    assert ids("quick fox", engine) == {"d01", "d09"}


def test_phrase_consecutive_positions(engine):
    assert ids('"quick brown"', engine) == {"d01", "d09"}
    # Term conjunction also happens to match the same two docs; but a term
    # AND ignores adjacency, whereas a phrase requires consecutive tokens.
    assert ids("quick brown", engine) == {"d01", "d09"}
    assert ids("waterfall quick", engine) == set()  # no doc has both


def test_field_phrase(engine):
    assert ids('body:"lazy dog"', engine) == {"d01", "d02"}


def test_chinese_unigram_phrase(engine):
    # 价格 tokenizes to [价, 格]; consecutive in body of d08 only.
    assert ids("body:价格", engine) == {"d08"}
    assert ids('body:"价格"', engine) == {"d08"}


def test_keyword_is_case_sensitive_exact(engine):
    assert ids("category:animals", engine) == {"d01", "d02", "d04", "d10", "d12"}
    assert ids("category:Animals", engine) == set()
    # substring is not a keyword match
    assert ids("category:animal", engine) == set()


def test_multi_valued_keyword(engine):
    assert ids("tags:winter", engine) == {"d03", "d07"}
    assert ids("tags:fox AND tags:dog", engine) == {"d01", "d12"}


def test_int_range_both_bounds(engine):
    assert ids("year:[2005 TO 2015]", engine) == {"d02", "d03", "d04"}


def test_int_range_open_upper(engine):
    assert ids("year:{2020 TO *]", engine) == {"d07", "d08", "d10", "d11"}


def test_date_range(engine):
    assert ids("published:[2020-01-01 TO 2020-12-31]", engine) == {"d06"}


def test_exclusive_int_bound(engine):
    assert ids("year:{2001 TO 2010}", engine) == {"d02"}


def test_not_is_set_complement_over_universe(engine):
    # NOT category:animals = every doc except the five animal ones.
    expected = {f"d{n:02d}" for n in range(1, 13)} - {
        "d01", "d02", "d04", "d10", "d12"
    }
    assert ids("NOT category:animals", engine) == expected
    # NOT never drops into "missing field = false"; d11 lacks category
    # and therefore appears in the complement.
    assert "d11" in ids("NOT category:animals", engine)


def test_and_or_precedence_end_to_end(engine):
    # (fox AND dog) OR salmon
    assert ids("fox AND dog OR salmon", engine) == {
        "d01", "d12",  # fox AND dog
        "d04", "d05", "d06", "d08",  # salmon in default fields
    }

def test_complement_collapses_to_empty_or_all(engine):
    assert ids("fox AND NOT fox", engine) == set()
    assert ids("fox OR NOT fox", engine) == {f"d{n:02d}" for n in range(1, 13)}


def test_missing_scalar_field_excluded_from_range(engine):
    # d11 has no 'published'; it must not match a date range.
    res = ids("published:[1900-01-01 TO 2100-12-31]", engine)
    assert "d11" not in res
    assert res == {"d01", "d02", "d03", "d04", "d05", "d06", "d07",
                   "d08", "d09", "d10", "d12"}


def test_empty_query_via_match_all_node(engine):
    from searchdsl.astnodes import MatchAll

    all_ids = {f"d{n:02d}" for n in range(1, 13)}
    assert set(execute(MatchAll(), engine.store, engine.schema).docs) == all_ids


def test_scores_rank_more_matching_leaves_first(engine):
    resp = engine.search("fox OR dog OR salmon", explain=False)
    assert resp.status == "ok"
    order = [r["doc_id"] for r in resp.results]
    scores = {r["doc_id"]: r["score"] for r in resp.results}
    # d01 has all three leaves (fox, dog, salmon? d01 body has no salmon).
    # Check monotonicity of returned order.
    ranked = [scores[d] for d in order]
    assert ranked == sorted(ranked, reverse=True)
    assert scores["d12"] == 2  # fox + dog


def test_pagination(engine):
    # All 12 docs lack year 1800 (d11 has 2024); NOT complements over the
    # universe so every doc matches.
    resp = engine.search("NOT year:1800", limit=5, offset=0)
    assert resp.total == 12
    page1 = [r["doc_id"] for r in resp.results]
    assert len(page1) == 5
    resp2 = engine.search("NOT year:1800", limit=5, offset=5)
    page2 = [r["doc_id"] for r in resp2.results]
    assert page1 != page2 and len(page2) == 5
