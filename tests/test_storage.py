"""Tests for the SQLite version store and repository contracts."""

from __future__ import annotations

import pytest

from app.errors import AlreadyAppliedError, PlanNotFound
from app.planner import RuleSpec, build_plan
from app.storage import Database, Repository
from app.textspec import ByteIndex, sha256_hex


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "test.db")
    yield Repository(db)
    db.close()


def _seed_source(repo, sid="s1", text="cat catalog"):
    data = text.encode()
    idx = ByteIndex(data)
    return repo.put_source(sid, data, codepoints=idx.codepoints, sha256=sha256_hex(data))


def test_source_versioning_bumps_and_is_content_addressed(repo, run_logger, request):
    v1 = _seed_source(repo, text="abc")
    v1b = _seed_source(repo, text="abc")  # identical bytes
    v2 = _seed_source(repo, text="abcd")
    assert v1.version == 1 and v1b.version == 1
    assert v2.version == 2
    assert v1.sha256 != v2.sha256
    # old immutable version still retrievable by digest
    assert repo.get_source_by_sha(v1.sha256) == b"abc"
    run_logger.check(
        request.node.nodeid,
        "source versioning",
        expected=(1, 2),
        actual=(v1b.version, v2.version),
        passed=(v1b.version, v2.version) == (1, 2),
        reason="identical re-upload is idempotent; changed bytes bump version; "
               "old content remains addressable by sha256",
    )


def test_ruleset_roundtrip_preserves_spec(repo):
    rules = [RuleSpec("a", r"a", "A", priority=2, flags="i"),
             RuleSpec("b", r"b", "B")]
    repo.put_ruleset("rs", rules)
    got = repo.get_ruleset("rs")
    assert len(got.rules) == 2
    assert got.rules[0].rule_id == "a"
    assert got.rules[0].flags == "i"
    assert got.rules[0].priority == 2


def test_plan_persistence_and_decisions(repo):
    _seed_source(repo, text="cat catalog")
    repo.put_ruleset("rs", [
        RuleSpec("cat", r"cat", "FELIX", priority=5),
        RuleSpec("catalog", r"catalog", "LOG", priority=1),
    ])
    data = repo.get_source("s1").data
    rules = list(repo.get_ruleset("rs").rules)
    result = build_plan(data, rules)
    stored = repo.put_plan("p1", result, ruleset_id="rs")
    assert stored.edit_count == result.plan.edit_count
    assert stored.candidates_dropped >= 1  # catalog suppressed
    decisions = repo.get_decisions("p1")
    assert any(d["stage"] == "reject" and d["rule_id"] == "catalog" for d in decisions)
    # round-trip plan JSON and re-apply identically
    from app.planner.model import Plan
    from app.planner import apply_plan_stream

    plan = Plan.from_json(stored.plan_json)
    out = apply_plan_stream(plan, data).output
    # 'catalog' is wholly rejected (same start as winning 'cat'), so the
    # untouched suffix 'alog' remains literal.
    assert out == b"FELIX FELIXalog"


def test_application_is_one_shot_per_plan(repo):
    _seed_source(repo, text="abc")
    repo.put_ruleset("rs", [RuleSpec("a", r"a", "X")])
    data = repo.get_source("s1").data
    result = build_plan(data, list(repo.get_ruleset("rs").rules))
    repo.put_plan("p1", result, ruleset_id="rs")
    repo.record_application(
        "app1", "p1", "s1", result.plan.source_sha256,
        sha256_hex(b"Xbc"), 3, 3, None,
    )
    with pytest.raises(AlreadyAppliedError) as exc:
        repo.record_application(
            "app2", "p1", "s1", result.plan.source_sha256,
            sha256_hex(b"Xbc"), 3, 3, None,
        )
    assert exc.value.code == "already_applied"
    assert repo.get_application_by_plan("p1").id == "app1"


def test_missing_plan_raises_not_found(repo):
    with pytest.raises(PlanNotFound) as exc:
        repo.get_plan("nope")
    assert exc.value.code == "plan_not_found"
    assert exc.value.http_status == 404


def test_persistence_across_connections(tmp_path):
    path = tmp_path / "persist.db"
    db = Database(path)
    repo = Repository(db)
    _seed_source(repo, text="hello")
    db.close()

    db2 = Database(path)
    repo2 = Repository(db2)
    assert repo2.get_source("s1").data == b"hello"
    db2.close()
