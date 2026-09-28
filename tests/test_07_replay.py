"""夹具离线回放测试：全用例通过、失败状态保全、日志可重放。"""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

from utxo_ledger.replay import replay_fixture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "validation_cases.json")


@pytest.fixture(scope="module")
def oracle():
    path = os.path.join(ROOT, "reference", "oracle.py")
    spec = importlib.util.spec_from_file_location("independent_oracle2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_fixture_cases_pass(oracle, log_dir):
    fx = json.load(open(FIXTURE, encoding="utf-8"))
    results = replay_fixture(fx, log_dir=log_dir, oracle=oracle)
    failures = [r for r in results if not r.oracle_match]
    assert not failures, [
        (r.name, r.error) for r in failures
    ]
    # 拒绝用例必须全部状态保全
    rejected = [r for r in results if not r.accepted]
    assert len(rejected) >= 10
    assert all(r.state_preserved is not False for r in rejected)


def test_expected_codes_match_fixture(oracle, log_dir):
    fx = json.load(open(FIXTURE, encoding="utf-8"))
    results = replay_fixture(fx, log_dir=log_dir, oracle=oracle)
    got = {r.name: r for r in results}
    for case in fx["cases"]:
        r = got[case["name"]]
        assert r.expected_accepted == case["expected_accepted"]
        if not case["expected_accepted"]:
            assert r.error["code"] == case["expected_code"]
            assert r.error["category"] == case["expected_category"]
            if "expected_tx_index" in case:
                assert r.error["tx_index"] == case["expected_tx_index"]


def test_journal_is_replayable_and_has_run_ids(oracle, log_dir):
    fx = json.load(open(FIXTURE, encoding="utf-8"))
    replay_fixture(fx, log_dir=log_dir, oracle=oracle)
    files = sorted(f for f in os.listdir(log_dir) if f.endswith(".jsonl"))
    assert files, "必须写出 JSONL 日志"
    # 每个 run_id 唯一，且摘要包含逐交易判定与失败理由
    run_ids = set()
    found_summary = 0
    for name in files:
        records = [
            json.loads(line)
            for line in open(os.path.join(log_dir, name), encoding="utf-8")
            if line.strip()
        ]
        run_ids.add(records[0]["run_id"])
        for r in records:
            if r.get("stage") == "run_summary":
                found_summary += 1
                if r["accepted"] is False:
                    assert r["failure"]["code"]
                    assert r["state_preserved_on_failure"] is True
    assert len(run_ids) == len(files)
    assert found_summary >= 1
