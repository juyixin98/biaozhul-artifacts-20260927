"""版本存储与计划绑定测试：源摘要守卫、版本不符拒绝、重复应用冲突、流式应用。"""
from __future__ import annotations

import pytest

from app.apply import apply_plan_stream
from app.diagnostics import Diag
from app.errors import (
    PlanAlreadyAppliedError,
    SourceVersionMismatchError,
    StateConflictError,
)
from app.schemas import PlanRequest, RuleIn
from app.service import Service
from app.storage import Repository
from app.textutil import make_spec


@pytest.fixture
def svc():
    repo = Repository(":memory:")
    yield Service(repo)
    repo.close()


def _mail_plan(svc: Service, text: str = "a@x.com b@y.org"):
    src = svc.upload_source(text)
    rules = [RuleIn(rule_id="mail", pattern=r"(\w+)@(\w+)\.(\w+)", template=r"[\1@\2.\3]")]
    summary, _ = svc.create_plan(PlanRequest(source_id=src["source_id"], rules=rules))
    return src, summary.plan_id


def test_plan_bound_to_source_digest_and_version(svc, record):
    src, plan_id = _mail_plan(svc)
    header = svc.repo.get_plan_header(plan_id)
    current = svc.get_source(src["source_id"])
    record.state(
        "binding",
        {"plan_version": header["source_version"], "sha256": header["sha256"]},
        "计划必须绑定创建时的版本号与源摘要",
    )
    assert header["source_version"] == current["version"]
    assert header["sha256"] == current["spec"]["sha256"]


def test_apply_streaming_matches_collect(svc, record):
    src, plan_id = _mail_plan(svc)
    diag = Diag(svc.repo)
    gen, meta, run_id = svc.apply_plan_stream(plan_id, diag, chunk_chars=3)  # 极小分片
    chunks = list(gen())
    streamed = "".join(chunks)
    record.state("chunk_count", len(chunks), "3 码点分片应产生多块")
    record.state("streamed", streamed)
    assert len(chunks) > 1
    assert streamed == "[a@x.com] [b@y.org]"

    text, ver, _ = svc.repo.get_text(src["source_id"])
    record.check("new current version is 2", ok=ver == 2, expected=2, actual=ver)
    assert ver == 2
    assert make_spec(streamed).sha256 == make_spec(text).sha256
    # 诊断里应能看到 run_id 的守卫与提交事件
    events = svc.repo.get_diag(run_id)
    stages = [(e["stage"], e["event"]) for e in events]
    record.state("diag_events", stages)
    assert ("apply", "guard_ok") in stages and ("apply", "committed") in stages


def test_version_mismatch_rejects_apply(svc, record):
    src, plan_id = _mail_plan(svc)
    # 在计划之后改写源 -> 新版本，内容不同
    svc.replace_source(src["source_id"], "totally different text now")
    diag = Diag(svc.repo)
    with pytest.raises(SourceVersionMismatchError) as ei:
        gen, _, _ = svc.apply_plan_stream(plan_id, diag)
        list(gen())
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "STATE_SOURCE_VERSION_MISMATCH"
    assert ei.value.details["current_version"] == 2
    assert ei.value.details["plan_bound_version"] == 1
    # 计划不得被标记为已应用
    assert svc.repo.get_plan_header(plan_id)["status"] == "planned"
    # 诊断记录了版本冲突
    events = svc.repo.get_diag(diag.run_id)
    assert any(e["event"] == "version_mismatch" for e in events)


def test_same_version_but_tampered_digest_rejects(svc, record):
    src, plan_id = _mail_plan(svc, "abcdef")
    # 直接在存储层篡改当前版本正文但不改版本号（异常情况），守卫必须靠摘要挡住
    import gzip
    tampered = gzip.compress("ABCDEF-tampered".encode("utf-8"))
    with svc.repo._lock:
        svc.repo._conn.execute(
            "UPDATE version_bodies SET gz_text=? WHERE source_id=? AND version=1",
            (tampered, src["source_id"]),
        )
        svc.repo._conn.commit()
    diag = Diag(svc.repo)
    with pytest.raises(SourceVersionMismatchError) as ei:
        gen, _, _ = svc.apply_plan_stream(plan_id, diag)
        list(gen())
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert ei.value.code == "STATE_SOURCE_VERSION_MISMATCH"
    assert "digest" in ei.value.message or "digest" in str(ei.value.details)


def test_double_apply_is_state_conflict(svc, record):
    _src, plan_id = _mail_plan(svc)
    svc.apply_plan_collect(plan_id)
    with pytest.raises(PlanAlreadyAppliedError) as ei:
        svc.apply_plan_collect(plan_id)
    record.fail_category(ei.value.code, ei.value.category, ei.value.http_status)
    assert isinstance(ei.value, StateConflictError)
    assert ei.value.http_status == 409


def test_unknown_source_and_plan_are_404(svc):
    from app.errors import PlanNotFoundError, SourceNotFoundError
    with pytest.raises(SourceNotFoundError):
        svc.get_source("nope")
    with pytest.raises(PlanNotFoundError):
        svc.get_plan_detail("plan-nope")


def test_source_versions_are_immutable(svc, record):
    src = svc.upload_source("v1 text")
    svc.replace_source(src["source_id"], "v2 text longer")
    svc.replace_source(src["source_id"], "v3")
    # 历史版本仍可取回且摘要稳定
    v1, meta1, spec1 = svc.repo.get_text(src["source_id"], 1)
    v2, _, _ = svc.repo.get_text(src["source_id"], 2)
    v3, cur, spec3 = svc.repo.get_text(src["source_id"], 3)
    record.state("versions", [v1, v2, v3])
    assert (v1, v2, v3) == ("v1 text", "v2 text longer", "v3")
    assert cur == 3
    assert make_spec("v1 text") == spec1
