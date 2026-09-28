"""Parser tests for rule/evidence input contracts."""
from __future__ import annotations

import pytest

from app.errors import InputError
from app.parsing import parse_evidence, parse_policy


def test_policy_must_be_object():
    with pytest.raises(InputError) as exc:
        parse_policy(["nope"])
    assert exc.value.code == "policy.not_object"


def test_policy_dimensions_required_and_path_required():
    with pytest.raises(InputError) as exc:
        parse_policy({"name": "p", "covered_dimensions": []})
    assert exc.value.code == "policy.dimensions_missing"

    with pytest.raises(InputError) as exc:
        parse_policy({"name": "p", "covered_dimensions": ["query"]})
    assert exc.value.code == "policy.path_required"


def test_policy_identity_mode_enumerated():
    with pytest.raises(InputError) as exc:
        parse_policy({"name": "p", "covered_dimensions": ["path"],
                      "identity": {"mode": "trust-me"}})
    assert exc.value.code == "policy.identity_mode"


def test_evidence_duplicate_request_ids():
    doc = {
        "requests": [
            {"id": "r1", "method": "GET", "path": "/a"},
            {"id": "r1", "method": "GET", "path": "/b"},
        ],
        "responses": [{"request_id": "r1", "status": 200}],
    }
    with pytest.raises(InputError) as exc:
        parse_evidence(doc)
    assert exc.value.code == "evidence.duplicate_request"


def test_evidence_path_must_be_absolute():
    doc = {
        "requests": [{"id": "r1", "method": "GET", "path": "relative/path"}],
        "responses": [{"request_id": "r1", "status": 200}],
    }
    with pytest.raises(InputError) as exc:
        parse_evidence(doc)
    assert exc.value.code == "evidence.path"


def test_query_list_normalised_to_sorted_pairs():
    reqs, _ = parse_evidence({
        "requests": [
            {"id": "a", "method": "GET", "path": "/x", "query": {"b": "2", "a": "1"}},
        ],
        "responses": [{"request_id": "a", "status": 200}],
    })
    assert reqs[0].query == (("a", "1"), ("b", "2"))


def test_headers_lowercased():
    reqs, resps = parse_evidence({
        "requests": [{"id": "a", "method": "GET", "path": "/x",
                      "headers": {"Accept-Language": "EN"}}],
        "responses": [{"request_id": "a", "status": 200,
                       "headers": {"Vary": "Accept-Language"}}],
    })
    assert reqs[0].headers == {"accept-language": "EN"}
    assert resps[0].vary == ("accept-language",)


def test_absent_vary_is_none_not_empty():
    _, resps = parse_evidence({
        "requests": [{"id": "a", "method": "GET", "path": "/x"}],
        "responses": [{"request_id": "a", "status": 200}],
    })
    assert resps[0].vary is None
