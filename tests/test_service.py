"""Service-level tests: concrete corrections, ranking stability, caps."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import QueryTooLongError, VersionNotFoundError


def test_recieve_corrects_to_receive_with_swap_path(service):
    report = service.correct("recieve", request_id="r1", threshold=2.0)
    token = report.token_reports[0]
    assert token.corrections[0].candidate == "receive"
    # Fixture weights the ie->ei swap at 0.75 (asymmetric pair cost).
    assert token.corrections[0].distance == 0.75
    assert token.corrections[0].path[0]["op"] == "transpose"
    assert token.corrections[0].path_matches is True
    assert report.corrected_text == "receive"
    assert report.version_id == service.store.active_version()


def test_teh_to_the_single_transpose(service):
    report = service.correct("teh", request_id="r2", threshold=1.5)
    c = report.token_reports[0].corrections[0]
    assert c.candidate == "the"
    assert c.path[0]["op"] == "transpose"
    assert c.path_recomputed_cost == c.distance


def test_ranking_distance_then_term_then_frequency(service):
    # "about" is in the fixture at high frequency; craft a query equidistant
    # to several words and check deterministic ordering across repeated runs.
    report1 = service.correct("abou", request_id="r3", threshold=1.5)
    candidates = [
        (c.candidate, c.distance) for c in report1.token_reports[0].corrections
    ]
    assert candidates  # non-empty
    assert candidates == sorted(candidates, key=lambda x: (x[1], x[0]))

    report2 = service.correct("abou", request_id="r4", threshold=1.5)
    assert [c.candidate for c in report1.token_reports[0].corrections] == [
        c.candidate for c in report2.token_reports[0].corrections
    ]
    # distance-1 winner must be "about".
    assert candidates[0] == ("about", 1.0)


def test_exact_match_preserved_in_corrected_text(service):
    report = service.correct("hello world", request_id="r5", threshold=1.0)
    assert report.corrected_text == "hello world"
    assert all(t.exact_match for t in report.token_reports)


def test_multi_token_offsets_and_partial_correction(service):
    report = service.correct("teh recieve", request_id="r6", threshold=1.5)
    assert report.normalized_query == "teh recieve"
    assert [t.token.text for t in report.token_reports] == ["teh", "recieve"]
    assert report.token_reports[0].token.start == 0
    assert report.token_reports[1].token.start == 4
    assert report.corrected_text == "the receive"


def test_threshold_filters_distant_candidates(service):
    strict = service.correct("levenshtein", request_id="r7", threshold=1.0)
    relaxed = service.correct("levenshtein", request_id="r8", threshold=2.0)
    strict_terms = {c.candidate for c in strict.token_reports[0].corrections}
    relaxed_terms = {c.candidate for c in relaxed.token_reports[0].corrections}
    assert strict_terms <= relaxed_terms
    assert "levenshtein" in strict_terms  # exact match present


def test_threshold_edge_value_included(service):
    # A word at exactly distance threshold must appear (boundary tolerance).
    report = service.correct("receiv", request_id="r9", threshold=1.0)
    terms = {c.candidate for c in report.token_reports[0].corrections}
    assert "receive" in terms  # insert one char -> exactly 1


def test_unknown_version_failure_category(service):
    with pytest.raises(VersionNotFoundError) as exc:
        service.correct("hello", request_id="r10", version_id=9999)
    assert exc.value.code == "version_not_found"


def test_query_length_cap_failure_category(service):
    with pytest.raises(QueryTooLongError) as exc:
        service.correct("x" * 65, request_id="r11")
    assert exc.value.code == "query_too_long"
    assert exc.value.details["limit"] == 64


def test_max_results_cap_marks_uncertainty(service):
    report = service.correct("there", request_id="r12", threshold=2.0, max_results=2)
    token = report.token_reports[0]
    assert len(token.corrections) <= 2
    assert any("only top 2 returned" in u for u in report.uncertainties)


def test_window_truncation_marks_uncertainty(settings, store):
    # Tiny scoring cap: force the length-window SQL limit to truncate.
    from app.config import Limits
    from app.service import SpellcheckService

    tight = Settings(
        db_path=settings.db_path,
        seed_file=settings.seed_file,
        alphabet=settings.alphabet,
        limits=Limits(
            max_query_chars=64,
            max_query_tokens=8,
            max_candidates_scored=2,
            max_results_per_token=10,
            max_search_nodes=60_000,
            default_threshold=2.0,
        ),
        costs=settings.costs,
    )
    svc = SpellcheckService(tight, store)
    report = svc.correct("the", request_id="r13", threshold=2.0)
    assert any("length window" in u for u in report.uncertainties)
