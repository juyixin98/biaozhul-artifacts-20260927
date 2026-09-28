"""SQLite 存储完整性测试：唯一索引、持久化、淘汰不破坏索引。"""

from __future__ import annotations

import pytest

from localtxpool.storage.repository import Repository, UniqueActiveViolation


def _base_fields(tx_hash="0x" + "aa" * 32, sender="0x" + "11" * 20, nonce=0,
                 status="pending"):
    return dict(
        tx_hash=tx_hash, raw=b"\xc0", sender=sender, to_addr=None, nonce=nonce,
        gas_price="10", gas_limit=21000, value="0", data=b"", chain_id=1,
        received_at=0, expires_at=9999, status=status, reason="R",
        reason_detail="", replaced_by=None, block_number=None, position=None,
        updated_at=0,
    )


def test_unique_active_sender_nonce_constraint():
    repo = Repository(":memory:")
    repo.insert_tx(_base_fields())
    # 同 sender+nonce 第二条活跃交易 -> 唯一索引拒绝
    with pytest.raises(UniqueActiveViolation):
        repo.insert_tx(_base_fields(tx_hash="0x" + "bb" * 32))


def test_inactive_status_releases_slot():
    repo = Repository(":memory:")
    repo.insert_tx(_base_fields())
    # 归档为 replaced 后槽位释放
    repo.set_tx_status("0x" + "aa" * 32, "replaced", "REASON", "", 0)
    repo.insert_tx(_base_fields(tx_hash="0x" + "bb" * 32))  # 不抛
    active = repo.list_sender("0x" + "11" * 20, ("pending", "queued", "included"))
    assert len(active) == 1 and active[0].tx_hash == "0x" + "bb" * 32


def test_expired_and_mined_do_not_collide_with_new():
    repo = Repository(":memory:")
    repo.insert_tx(_base_fields(status="pending"))
    repo.set_tx_status("0x" + "aa" * 32, "mined", "REASON", "", 0, block_number=1)
    repo.insert_tx(_base_fields(tx_hash="0x" + "cc" * 32))
    repo.set_tx_status("0x" + "cc" * 32, "expired", "REASON", "", 0)
    # 再来一条同 nonce 仍可插入（前两条都已不占活跃槽位）
    repo.insert_tx(_base_fields(tx_hash="0x" + "dd" * 32))
    allrows = repo.list_all(
        ("pending", "queued", "included", "mined", "expired", "replaced", "evicted"))
    assert len(allrows) == 3


def test_cheapest_queued_ordering_is_deterministic():
    repo = Repository(":memory:")
    for i, gp in enumerate([30, 10, 20]):
        repo.insert_tx(_base_fields(
            tx_hash="0x" + f"{i:064x}", sender="0x" + f"{i:040x}",
            nonce=0, status="queued"))
        repo.set_tx_status(f"0x{i:064x}", "queued", "R", "", 0)
        # 直接更新 gas_price（测试便利性）
        repo._conn.execute("UPDATE transactions SET gas_price=? WHERE tx_hash=?",
                           (str(gp), f"0x{i:064x}"))
    cheap = repo.cheapest_queued()
    assert cheap.gas_price == 10


def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / "p.db")
    repo = Repository(path)
    repo.insert_tx(_base_fields())
    repo.set_meta("k", "v")
    repo.close()
    repo2 = Repository(path)
    assert repo2.get_meta("k") == "v"
    assert repo2.count_status(("pending",)) == 1
    repo2.close()


def test_transaction_rollback_on_error():
    repo = Repository(":memory:")
    repo.insert_tx(_base_fields())
    with pytest.raises(UniqueActiveViolation):
        with repo.transaction():
            repo.insert_tx(_base_fields(tx_hash="0x" + "ee" * 32,
                                        status="queued"))  # OK so far
            repo.insert_tx(_base_fields(tx_hash="0x" + "ff" * 32))  # 冲突 -> 回滚
    # 同一事务内第一条也必须被回滚
    assert repo.get_tx("0x" + "ee" * 32) is None
    assert repo.count_status(("pending", "queued")) == 1


def test_journal_query_and_correlation():
    repo = Repository(":memory:")
    repo.add_journal(ts=1, request_id="req-A", action="receive",
                     tx_hash="0x" + "ab" * 32, sender="0x" + "11" * 20,
                     from_status="", to_status="queued", reason="RECEIVED",
                     detail={"gas_price": 10})
    rows = repo.query_journals(request_id="req-A")
    assert len(rows) == 1 and rows[0]["action"] == "receive"
    detail = rows[0]["detail"]
    import json
    assert json.loads(detail)["gas_price"] == 10
