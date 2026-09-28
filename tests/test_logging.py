"""日志可关联性：run_id/指纹/版本/步骤与判定依据落在结构化日志中。"""

from __future__ import annotations

import json
from pathlib import Path

from app import __version__
from app.core.logging_setup import _input_fp_var, _run_id_var, bind_run, current_run_id
from tests.conftest import load_fixture, make_payload


def _read_jsonl(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_run_logs_carry_run_id_fingerprint_version_and_steps(state, settings):
    fx = load_fixture("tiny_patients")
    resp = state.service.analyze(make_payload(fx, 2, 2))

    records = _read_jsonl(settings.app_log_path)
    by_run = [r for r in records if r.get("run_id") == resp.run_id]

    assert by_run, "no structured log records for this run_id"
    assert all(r["service_version"] == __version__ for r in by_run)
    fp_records = [r for r in by_run if r.get("input_fingerprint")]
    assert fp_records
    step_events = [r for r in by_run if r.get("event", "").startswith("step:")]
    assert len(step_events) >= 2
    stage_names = {r.get("stage") for r in step_events}
    assert {"search_init", "evaluate_top", "verdict"} <= stage_names
    verdict = [r for r in by_run if r.get("event") == "verdict"][0]
    assert verdict["status"] == "succeeded"
    assert verdict["reason"]


def test_failed_run_logs_status_not_succeeded(state, settings):
    fx = load_fixture("unique_signatures")
    resp = state.service.analyze(make_payload(fx, 2, 1))
    assert resp.status == "k_unreachable"

    records = _read_jsonl(settings.app_log_path)
    verdicts = [
        r for r in records
        if r.get("run_id") == resp.run_id and r.get("event") == "verdict"
    ]
    assert verdicts and verdicts[0]["status"] == "k_unreachable"


def test_context_binding_helper():
    t1, t2 = bind_run(run_id="run_test123", input_fingerprint="fp")
    try:
        assert current_run_id() == "run_test123"
    finally:
        _run_id_var.reset(t1)
        _input_fp_var.reset(t2)
