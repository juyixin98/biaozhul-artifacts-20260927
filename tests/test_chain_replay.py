"""链状态内核 + 离线回放测试：bundle 对拍、失败不转账、失败分类。"""

from __future__ import annotations

import pytest

from rsv.chain import ChainKernel
from rsv.config import load_settings
from rsv.errors import VerificationFailure
from rsv.replay import replay_bundle
from rsv.storage import SQLiteStore
from tests.conftest import load_bundle


def _kernel(tmp_path) -> ChainKernel:
    settings = load_settings(
        sqlite_path=str(tmp_path / "test.sqlite3"),
        runs_dir=str(tmp_path / "runs"),
    )
    return ChainKernel(settings, store=SQLiteStore(settings.sqlite_path))


def test_happy_path_bundle_matches_reference(genesis, tmp_path):
    bundle = load_bundle("happy_path.json")
    k = _kernel(tmp_path)
    k.bootstrap_from_dict(genesis)
    before = k.store.state_root()
    utxo_before = {(u.txid, u.idx) for u in k.store.list_utxos()}

    for tx, exp in zip(bundle["transactions"], bundle["expected"]):
        rep = k.verify_transaction_dict(tx, persist=True, kind="submit")
        assert rep.accepted is exp["accepted"]
        assert rep.failure is None
        assert rep.message32 and len(rep.message32) == 64

    after = k.store.state_root()
    assert before != after  # 状态确实变了
    utxo_after = {(u.txid, u.idx) for u in k.store.list_utxos()}
    assert utxo_before != utxo_after
    # 被花掉的是 domain A 的 3 枚币；domain B 的币保持不变（共 4 枚 UTXO）
    assert len(k.store.list_utxos()) == 4
    # 金额总量守恒（含未被动过的 domain B 500）
    total = sum(u.value for u in k.store.list_utxos())
    assert total == 1000 + 2000 + 3000 + 500
    # domain A 里只剩本 bundle 三笔交易各产生的一枚新 UTXO，且 txid 与内核独立计算一致
    from rsv.encoding.transaction import transaction_from_dict
    new_a = k.store.list_utxos(domain="rsv-test-domain-v1")
    assert len(new_a) == 3
    expected_txids = {
        transaction_from_dict(t, k.settings.chain).txid().hex()
        for t in bundle["transactions"]
    }
    assert {u.txid for u in new_a} == expected_txids


def test_failure_catalog_each_classification(genesis, tmp_path):
    bundle = load_bundle("failure_catalog.json")
    k = _kernel(tmp_path)
    k.bootstrap_from_dict(genesis)
    for tx, exp in zip(bundle["transactions"], bundle["expected"]):
        rep = k.verify_transaction_dict(tx, persist=True, kind="submit")
        assert rep.accepted is exp["accepted"], (rep.failure, exp)
        if not exp["accepted"]:
            assert rep.failure is not None
            assert rep.failure["code"] == exp["code"], (rep.failure, exp)


def test_double_spend_second_rejected(genesis, tmp_path):
    bundle = load_bundle("double_spend.json")
    k = _kernel(tmp_path)
    k.bootstrap_from_dict(genesis)
    r1 = k.verify_transaction_dict(bundle["transactions"][0], persist=True)
    assert r1.accepted
    root1 = k.store.state_root()
    r2 = k.verify_transaction_dict(bundle["transactions"][1], persist=True)
    assert not r2.accepted
    assert r2.failure["category"] == "state"
    assert r2.failure["code"] == "state.already_spent"
    # 关键不变量：被拒后状态根、UTXO 集合完全不变
    assert k.store.state_root() == root1


@pytest.mark.parametrize("bundle_name", ["happy_path.json", "failure_catalog.json",
                                         "double_spend.json"])
def test_offline_replay_matches_independent_expected(bundle_name, tmp_path):
    bundle = load_bundle(bundle_name)
    res = replay_bundle(bundle, runs_dir=str(tmp_path / "replay-runs"))
    seq_results = [r for r in res.results if r.get("phase") != "bootstrap"]
    for got, exp in zip(seq_results, bundle["expected"]):
        assert got["accepted"] is exp["accepted"], (bundle_name, got)
        if not exp["accepted"]:
            assert got["failure"]["code"] == exp["code"], (bundle_name, got, exp)
    assert res.accepted_count == sum(1 for e in bundle["expected"] if e["accepted"])
    assert res.rejected_count == sum(1 for e in bundle["expected"] if not e["accepted"])


def test_replay_is_deterministic(tmp_path):
    bundle = load_bundle("happy_path.json")
    r1 = replay_bundle(bundle, runs_dir=str(tmp_path / "a"))
    r2 = replay_bundle(bundle, runs_dir=str(tmp_path / "b"))
    # 忽略 run_id/时间戳等非确定字段
    def sig(res):
        tx_rows = [x for x in res.results if "phase" not in x]
        return [(x["seq"], x["accepted"], x.get("failure", {}).get("code")) for x in tx_rows]
    assert sig(r1) == sig(r2)
    assert r1.final_state_root == r2.final_state_root


def test_verify_dry_run_does_not_change_state(genesis, tmp_path):
    bundle = load_bundle("happy_path.json")
    k = _kernel(tmp_path)
    k.bootstrap_from_dict(genesis)
    root0 = k.store.state_root()
    n0 = len(k.store.list_utxos())
    for tx in bundle["transactions"]:
        rep = k.verify_transaction_dict(tx, persist=False)  # dry-run
        assert rep.accepted
        assert k.store.state_root() == root0  # 始终不变
        assert len(k.store.list_utxos()) == n0


def test_rejected_tx_changes_nothing(genesis, tmp_path):
    """每条失败交易前后的 UTXO 集合、金额、状态根必须逐字节相同。"""
    bundle = load_bundle("failure_catalog.json")
    k = _kernel(tmp_path)
    k.bootstrap_from_dict(genesis)
    for tx in bundle["transactions"]:
        snap = sorted((u.txid, u.idx, u.value) for u in k.store.list_utxos())
        root = k.store.state_root()
        rep = k.verify_transaction_dict(tx, persist=True)
        assert not rep.accepted
        snap2 = sorted((u.txid, u.idx, u.value) for u in k.store.list_utxos())
        assert snap == snap2
        assert k.store.state_root() == root


def test_domain_conflict_is_state_not_crypto(genesis, tmp_path):
    bundle = load_bundle("failure_catalog.json")
    wdom = bundle["transactions"][2]  # 用 domain A 花 domain B 的币
    k = _kernel(tmp_path)
    k.bootstrap_from_dict(genesis)
    rep = k.verify_transaction_dict(wdom, persist=False)
    assert rep.failure["category"] == "state"
    assert rep.failure["code"] == "state.domain_conflict"


def test_malformed_tx_classified(tmp_path):
    k = _kernel(tmp_path)
    rep = k.verify_transaction_dict({"version": 1, "inputs": [], "outputs": []}, persist=False)
    assert not rep.accepted
    assert rep.failure["category"] == "input"


def test_runs_persist_with_run_id_and_reason(genesis, tmp_path):
    bundle = load_bundle("double_spend.json")
    k = _kernel(tmp_path)
    k.bootstrap_from_dict(genesis)
    r1 = k.verify_transaction_dict(bundle["transactions"][0], persist=True)
    r2 = k.verify_transaction_dict(bundle["transactions"][1], persist=True)
    # run_id 可回填查询，保留判断理由与类别
    got1 = k.store.get_run(r1.run_id)
    got2 = k.store.get_run(r2.run_id)
    assert got1 is not None and got1.accepted
    assert got2 is not None and not got2.accepted
    assert got2.code == "state.already_spent"
    assert "未转账" in got2.reason
    assert got2.category == "state"
