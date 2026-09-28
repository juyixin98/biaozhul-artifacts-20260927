"""链状态内核、SQLite 存储原子性、离线回放测试。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from chain.replay import replay
from stackvm.errors import FailCode
from stackvm.transaction import transaction_from_dict, txid_of


def test_bootstrap_twice_rejected(store, genesis, settings):
    with pytest.raises(Exception) as ei:
        store.bootstrap_genesis(genesis, settings.chain.mint_total_cap)
    assert ei.value.code is FailCode.TX_ALREADY_ACCEPTED


def test_genesis_over_mint_cap_rejected(tmp_db, genesis, settings):
    from chain.store import Store
    s = Store(tmp_db)
    with pytest.raises(Exception) as ei:
        s.bootstrap_genesis(genesis, mint_cap=1000)
    assert ei.value.code is FailCode.TX_MALFORMED
    s.close()


def test_submit_ok_moves_utxo_atomically(store, kernel, cases_doc):
    case = cases_doc["cases"]["00"]
    tx = transaction_from_dict(case["tx"])
    outpoint = (case["tx"]["inputs"][0]["txid"], 0)
    assert store.get_utxo(*outpoint) is not None

    report = kernel.submit(tx)
    assert report.accepted is True
    # 旧 UTXO 被花费，新 UTXO 入集
    assert store.get_utxo(*outpoint) is None
    assert store.get_utxo(report.txid, 0) is not None
    assert store.has_transaction(report.txid)
    assert store.chain_height == 2
    # 金额守恒
    assert sum(u["value"] for u in store.list_utxos()) == 21_000


def test_submit_idempotent_second_time_state_conflict(store, kernel, cases_doc):
    case = cases_doc["cases"]["01"]
    tx = transaction_from_dict(case["tx"])
    first = kernel.submit(tx)
    assert first.accepted
    again = kernel.submit(tx)
    assert again.accepted is False
    assert again.code is FailCode.TX_ALREADY_ACCEPTED
    assert again.kind.value == "STATE"


def test_double_spend_after_success_is_utxo_missing(store, kernel, cases_doc):
    tx = transaction_from_dict(cases_doc["cases"]["02"]["tx"])
    assert kernel.submit(tx).accepted
    # 另一笔结构合法的交易引用同一 outpoint
    dup = json.loads(json.dumps(cases_doc["cases"]["02"]["tx"]))
    # 改动输出收款脚本使其成为不同 txid（仍平衡），引用不变
    dup["outputs"][0]["script"] = "00"
    report = kernel.submit(transaction_from_dict(dup))
    assert report.code is FailCode.UTXO_MISSING


def test_failed_validation_changes_nothing(store, kernel, cases_doc):
    snapshot = {
        "height": store.chain_height,
        "utxos": sorted((u["txid"], u["vout"], u["value"]) for u in store.list_utxos()),
        "root": store.state_root,
    }
    for cid in ["03", "05", "06", "07", "08", "09", "11", "15", "16", "17"]:
        before_journal = store.conn.execute("SELECT COUNT(*) c FROM journal").fetchone()["c"]
        tx = transaction_from_dict(cases_doc["cases"][cid]["tx"])
        report = kernel.submit(tx)
        assert report.accepted is False
        after_journal = store.conn.execute("SELECT COUNT(*) c FROM journal").fetchone()["c"]
        assert after_journal == before_journal, f"{cid} 失败时写了日志"
    assert store.chain_height == snapshot["height"]
    assert sorted((u["txid"], u["vout"], u["value"]) for u in store.list_utxos()) \
        == snapshot["utxos"]
    assert store.state_root == snapshot["root"]


def test_value_imbalance_classified(store, kernel, cases_doc):
    tx = json.loads(json.dumps(cases_doc["cases"]["00"]["tx"]))
    tx["outputs"][0]["value"] = 999
    report = kernel.submit(transaction_from_dict(tx))
    assert report.code is FailCode.VALUE_IMBALANCE
    assert report.kind.value == "COMPUTE"


def test_missing_utxo_input(store, kernel, cases_doc):
    tx = json.loads(json.dumps(cases_doc["cases"]["00"]["tx"]))
    tx["inputs"][0]["txid"] = "11" * 32
    report = kernel.submit(transaction_from_dict(tx))
    assert report.code is FailCode.UTXO_MISSING
    assert report.kind.value == "STATE"


def test_duplicate_input_reference_rejected(store, kernel, cases_doc):
    tx_dict = json.loads(json.dumps(cases_doc["cases"]["00"]["tx"]))
    tx_dict["inputs"].append(dict(tx_dict["inputs"][0]))
    # 金额仍平衡会在状态阶段被“重复引用”拦下
    report = kernel.evaluate(transaction_from_dict(tx_dict))
    assert report.code is FailCode.TX_MALFORMED


# ------------------------------ 日志与回放 ------------------------------

def test_journal_hash_chain_intact(store, kernel, cases_doc):
    for cid in ["00", "02", "04"]:
        assert kernel.submit(transaction_from_dict(cases_doc["cases"][cid]["tx"])).accepted
    store.verify_journal()  # 不抛异常即通过


def test_replay_matches_state_root(store, kernel, cases_doc, tmp_path):
    for cid in ["00", "01", "02", "10", "18"]:
        assert kernel.submit(transaction_from_dict(cases_doc["cases"][cid]["tx"])).accepted
    rebuilt = tmp_path / "rebuilt.db"
    report = replay(store.db_path, rebuild_to=rebuilt)
    assert report.ok, report.detail
    assert report.rebuilt_state_root == store.state_root
    assert report.applied == 5
    # 重建库真实存在且可独立打开
    conn = sqlite3.connect(str(rebuilt))
    assert conn.execute("SELECT COUNT(*) FROM utxos").fetchone()[0] == report.utxo_count
    conn.close()


def test_replay_detects_tampered_payload(store, kernel, cases_doc):
    assert kernel.submit(transaction_from_dict(cases_doc["cases"]["00"]["tx"])).accepted
    # 直接篡改日志负载
    store.conn.execute("UPDATE journal SET payload=REPLACE(payload,'1000','9999') WHERE seq=1")
    report = replay(store.db_path)
    assert not report.ok
    assert report.code is FailCode.JOURNAL_CORRUPT


def test_replay_detects_broken_chain(store, kernel, cases_doc):
    assert kernel.submit(transaction_from_dict(cases_doc["cases"]["00"]["tx"])).accepted
    store.conn.execute("UPDATE journal SET prev_hash=? WHERE seq=1", ("ff" * 32,))
    report = replay(store.db_path)
    assert report.code is FailCode.JOURNAL_CORRUPT


def test_state_root_mismatch_when_index_forks(store, kernel, cases_doc):
    assert kernel.submit(transaction_from_dict(cases_doc["cases"]["00"]["tx"])).accepted
    # 模拟索引存储与日志分叉：改 UTXO 金额，并把 meta 里的状态根同步改成
    # “当前被篡改索引”的根（否则单改 UTXO 只会留下一条过期根）。
    store.conn.execute("UPDATE utxos SET value=4242 WHERE vout=0")
    store.conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('state_root',?)",
        (store.compute_state_root(),))
    store.conn.commit()
    report = replay(store.db_path)
    assert report.code is FailCode.STATE_ROOT_MISMATCH


def test_txid_is_stable_and_unique(store, kernel, cases_doc):
    a = transaction_from_dict(cases_doc["cases"]["00"]["tx"])
    b = transaction_from_dict(cases_doc["cases"]["00"]["tx"])
    assert txid_of(a) == txid_of(b)
    assert len(txid_of(a)) == 64
