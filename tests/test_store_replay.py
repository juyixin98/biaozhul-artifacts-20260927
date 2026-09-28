"""索引存储与离线回放核验。

覆盖：

* 区块 / 收据落库与按哈希查询、链尖推进；
* 离线回放逐笔 ACCEPT；
* 篡改存证后回放必须 REJECT（状态根 / 费用 / 失败类别各构造一例）；
* 不同 program_version 的存证 => UNDETERMINED；
* 回放报告摘要在独立进程里一致。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3

import pytest

from teaching_chain.kernel import ChainState
from teaching_chain.replay import (
    ACCEPT,
    REJECT,
    UNDETERMINED,
    replay,
    report_to_json,
)
from teaching_chain.store import IndexStore
from teaching_chain.vm import assemble

from .conftest import asm, make_tx


def _build_db(path, alice, bob):
    node_state = ChainState()
    txs = [
        make_tx(alice, asm("PUSH8 11\nPUSH8 1\nSSTORE\nSTOP"), nonce=1),
        make_tx(bob, asm("PUSH8 1\nPUSH8 0\nDIV\nSTOP"), nonce=2),
        make_tx(alice, asm(
            "PUSH8 22\nPUSH8 2\nSSTORE\n"
            "PUSH8 8\nPUSH8 0\nMSTORE\nSTOP"), nonce=3),
    ]
    block, processed = node_state.apply_block(txs)
    store = IndexStore(path)
    store.initialize("teaching-chain-local")
    store.append_block(block)
    return store, txs, block, processed


def test_append_and_query_roundtrip(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, txs, block, processed = _build_db(db, alice, bob)
    assert store.head() == (0, block.hash())
    header = store.get_block_header(0)
    assert header["number"] == 0
    assert header["tx_count"] == 3
    receipt = store.get_receipt_by_tx_hash(processed[1].receipt.tx_hash)
    assert receipt["status"] == 0
    assert receipt["error_category"] == "DIV_BY_ZERO"
    assert store.integrity_check() == []
    store.close()


def test_append_rejects_non_contiguous_block(tmp_path, alice):
    store = IndexStore(tmp_path / "idx.db")
    store.initialize("teaching-chain-local")
    state = ChainState()
    block, _ = state.apply_block([make_tx(alice, asm("STOP"), nonce=1)])
    block.number = 5  # 人为跳号
    with pytest.raises(Exception):
        store.append_block(block)
    store.close()


def test_chain_name_mismatch_refuses_reuse(tmp_path, alice):
    db = tmp_path / "idx.db"
    s1 = IndexStore(db)
    s1.initialize("chain-a")
    s1.close()
    s2 = IndexStore(db)
    with pytest.raises(Exception):
        s2.initialize("chain-b")
    s2.close()


def test_replay_all_accept_on_clean_store(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, _, _, _ = _build_db(db, alice, bob)
    report = replay(store)
    assert report.block_count == 1
    assert report.tx_count == 3
    assert report.accepted == 3 and report.rejected == 0
    assert report.block_problems == []
    # 最终状态：失败的第二笔回滚，1、3 笔生效
    assert report.final_state_root
    checks = {c.tx_index: c for c in report.checks}
    assert checks[0].stored_status == 1
    assert checks[1].stored_status == 0
    assert checks[1].replayed_error == "DIV_BY_ZERO"
    assert checks[1].state_rolled_back is True
    store.close()


def test_replay_detects_tampered_state_root(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, _, block, _ = _build_db(db, alice, bob)
    store.close()

    # 直接篡改 SQLite 中某收据的 state_root
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT receipt_json FROM receipts WHERE tx_index=0").fetchone()
    data = json.loads(row[0])
    data["state_root"] = "ff" * 32
    conn.execute(
        "UPDATE receipts SET receipt_json=? WHERE tx_index=0",
        (json.dumps(data, sort_keys=True),),
    )
    conn.commit()
    conn.close()

    with IndexStore(db) as store2:
        report = replay(store2)
    assert report.rejected == 1
    check = report.checks[0]
    assert check.verdict == REJECT
    assert any("状态根不一致" in r for r in check.reasons)


def test_replay_detects_tampered_gas_used(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, _, _, _ = _build_db(db, alice, bob)
    store.close()
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT receipt_json FROM receipts WHERE tx_index=2").fetchone()
    data = json.loads(row[0])
    data["gas_used"] += 1
    conn.execute(
        "UPDATE receipts SET receipt_json=? WHERE tx_index=2",
        (json.dumps(data, sort_keys=True),),
    )
    conn.commit()
    conn.close()
    with IndexStore(db) as store2:
        report = replay(store2)
    assert any(
        c.verdict == REJECT and any("费用消耗不一致" in r for r in c.reasons)
        for c in report.checks
    )


def test_replay_detects_tampered_error_category(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, _, _, _ = _build_db(db, alice, bob)
    store.close()
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT receipt_json FROM receipts WHERE tx_index=1").fetchone()
    data = json.loads(row[0])
    data["error_category"] = "REVERTED"  # 原本是 DIV_BY_ZERO
    conn.execute(
        "UPDATE receipts SET receipt_json=? WHERE tx_index=1",
        (json.dumps(data, sort_keys=True),),
    )
    conn.commit()
    conn.close()
    with IndexStore(db) as store2:
        report = replay(store2)
    check = report.checks[1]
    assert check.verdict == REJECT
    assert any("失败类别不一致" in r for r in check.reasons)


def test_replay_undetermined_on_different_program_version(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, _, _, _ = _build_db(db, alice, bob)
    store.close()
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT receipt_json FROM receipts WHERE tx_index=0").fetchone()
    data = json.loads(row[0])
    data["program_version"] = "teaching-chain-vm/0.9.0-other"
    conn.execute(
        "UPDATE receipts SET receipt_json=? WHERE tx_index=0",
        (json.dumps(data, sort_keys=True),),
    )
    conn.commit()
    conn.close()
    with IndexStore(db) as store2:
        report = replay(store2)
    check = report.checks[0]
    assert check.verdict == UNDETERMINED
    assert any("程序版本" in r for r in check.reasons)
    assert report.undetermined == 1


def test_replay_detects_broken_parent_link(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, _, _, _ = _build_db(db, alice, bob)
    store.close()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE blocks SET parent_hash=? WHERE number=0", ("ee" * 32,))
    conn.commit()
    conn.close()
    with IndexStore(db) as store2:
        report = replay(store2)
    assert any("父链接断裂" in p for p in report.block_problems)


# ---------------------------------------------------------------------------
# 跨进程回放报告一致性
# ---------------------------------------------------------------------------
def _worker_report(db_path: str, q: mp.Queue) -> None:
    with IndexStore(db_path) as store:
        report = replay(store)
    q.put({"digest": report.digest(), "summary": report.to_dict()["summary"],
           "final_state_root": report.final_state_root})


def test_replay_report_identical_in_fresh_process(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    with IndexStore(db) as store:
        _, _, _, _ = _build_db(db, alice, bob)
        local = replay(store)

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    p = ctx.Process(target=_worker_report, args=(str(db), q))
    p.start()
    remote = q.get(timeout=30)
    p.join(timeout=30)
    assert p.exitcode == 0
    assert remote["digest"] == local.digest()
    assert remote["summary"] == local.to_dict()["summary"]
    assert remote["final_state_root"] == local.final_state_root


def test_report_json_is_stable_and_parseable(tmp_path, alice, bob):
    db = tmp_path / "idx.db"
    store, _, _, _ = _build_db(db, alice, bob)
    report = replay(store)
    text1 = report_to_json(report)
    text2 = report_to_json(report)
    assert text1 == text2  # 确定性序列化
    parsed = json.loads(text1)
    assert parsed["report_digest"] == report.digest()
    store.close()
