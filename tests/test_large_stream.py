"""Large-text streaming and resource-limit classification tests."""

from __future__ import annotations

import pytest

from app.api.service import Service, ServiceConfig
from app.errors import (
    MatchBudgetExceededError,
    OutputBudgetExceededError,
    TextTooLargeError,
)
from app.planner import PlannerLimits, RuleSpec, build_plan
from app.planner.apply import iter_plan_chunks
from app.storage import Database, Repository
from tests.oracle import reference_apply


@pytest.fixture()
def service(tmp_path):
    db = Database(tmp_path / "large.db")
    # ~150KB document -> ~25k edits, comfortably inside default caps, while
    # still spanning multiple 64KiB streaming chunks.
    yield Service(Repository(db), ServiceConfig())
    db.close()


def test_large_document_streaming_chunks_and_reference(service):
    # ~150 KB multibyte document, repeated deterministic lines; spans many
    # 64KiB apply chunks while remaining a fast reference comparison.
    line = "the cat sat on the mat 世界 €\n"
    text = (line * 5_000).rstrip("\n")
    rules = [
        {"rule_id": "cat", "pattern": r"cat", "template": "FELIX", "priority": 5},
        {"rule_id": "at", "pattern": r"at", "template": "@@"},
        {"rule_id": "cjk", "pattern": r"[世界]", "template": "?"},
        {"rule_id": "euro", "pattern": r"€", "template": "EUR"},
    ]
    from app.planner.rules import RuleSpec as RS

    specs = [RS(rule_id=r["rule_id"], pattern=r["pattern"], template=r["template"],
                priority=r.get("priority", 0)) for r in rules]

    stored = service.upload_source("big", text)
    service.repo.put_ruleset("rs", specs)
    stored_plan, result = service.create_plan("big", "rs")

    # verify against the independent reference on the same document
    ref = reference_apply(text, rules)

    record, output = service.apply_plan(stored_plan.id)
    # the service's configured chunk size governs streaming; assert output
    # agreement with the oracle and sanity on chunk count below.
    assert output.decode("utf-8") == ref
    assert record.result_sha256  # bound digest present
    # chunking: output is > one chunk; many chunks expected and deterministic
    # streaming used more than one chunk (default chunk is 64 KiB, output ~1MB)
    assert record.chunks_emitted > 1
    assert record.result_length == len(ref.encode("utf-8"))


def test_streaming_iterator_is_memory_bounded_and_bound(service):
    text = "abcdefgh" * 2_000  # 16 KB: many 4KiB chunks, no expensive fan-out
    service.upload_source("s", text)
    service.repo.put_ruleset("rs", [RuleSpec("z", r"z", "X")])  # no matches
    stored_plan, _ = service.create_plan("s", "rs")
    from app.planner.model import Plan

    plan = Plan.from_json(stored_plan.plan_json)
    data = service.repo.get_source_by_sha(stored_plan.source_sha256)
    sizes = [len(c) for c in iter_plan_chunks(plan, data, chunk_size=4096)]
    assert sizes  # produced something
    assert max(sizes) <= 4096
    assert sum(sizes) == len(data)  # no matches -> byte-for-byte identical


def test_source_size_cap_is_input_category(tmp_path):
    db = Database(":memory:")
    svc = Service(Repository(db), ServiceConfig(max_source_bytes=10))
    with pytest.raises(TextTooLargeError) as exc:
        svc.upload_source("s", "x" * 11)
    assert exc.value.code == "text_too_large"
    assert exc.value.category == "INPUT"


def test_match_budget_is_resource_category():
    with pytest.raises(MatchBudgetExceededError) as exc:
        build_plan(
            b"a" * 100,
            [RuleSpec("a", r"a", "X")],
            limits=PlannerLimits(max_edits=5),
        )
    assert exc.value.category == "RESOURCE"


def test_output_size_ceiling():
    # "ab"*50 = 100 bytes, 50 matches; each a -> AA adds one byte, so the
    # projected output is 150 bytes.
    with pytest.raises(OutputBudgetExceededError) as exc:
        build_plan(
            b"ab" * 50,
            [RuleSpec("a", r"a", "AA")],
            limits=PlannerLimits(max_output_bytes=120),
        )
    assert exc.value.code == "output_budget_exceeded"
    assert exc.value.details["projected"] == 150
