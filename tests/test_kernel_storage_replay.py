"""链状态内核、存储与离线回放测试。"""

from __future__ import annotations

import pytest

from app.abi import encode_call
from app.kernel import ChainKernel
from app.replay import ReplayError, replay
from app.storage import Storage

ALICE = b"\xaa" * 20
BOB = b"\xbb" * 20


def _cd(sig, *args):
    return encode_call(sig, tuple(args))


def test_mint_and_balance():
    k = ChainKernel()
    rcpt, _ = k.apply_tx(_cd("mint(bytes20,uint256)", ALICE, 100))
    assert rcpt.status == "ok"
    assert k.balance_of(ALICE) == 100


def test_transfer_success_and_insufficient_revert():
    k = ChainKernel()
    k.apply_tx(_cd("mint(bytes20,uint256)", ALICE, 100))
    ok, _ = k.apply_tx(_cd("transfer(bytes20,bytes20,uint256)", ALICE, BOB, 30))
    assert ok.status == "ok"
    assert k.balance_of(ALICE) == 70 and k.balance_of(BOB) == 30

    bad, _ = k.apply_tx(
        _cd("transfer(bytes20,bytes20,uint256)", BOB, ALICE, 999)
    )
    # 明确失败类别，且状态不变
    assert bad.status == "reverted"
    assert bad.error_category == "insufficient_balance"
    assert k.balance_of(ALICE) == 70 and k.balance_of(BOB) == 30


def test_malformed_calldata_is_reverted_not_crash():
    k = ChainKernel()
    rcpt, _ = k.apply_tx(b"\xde\xad\xbe\xef")  # 仅选择器、无体
    assert rcpt.status == "reverted"
    assert rcpt.error_category  # 必须有类别，不能是成功


def test_set_note_and_view():
    k = ChainKernel()
    k.apply_tx(_cd("setNote(bytes20,string)", ALICE, "你好"))
    rcpt, view = k.apply_tx(_cd("noteOf(bytes20)", ALICE))
    assert rcpt.status == "ok"
    assert view == "你好"


def test_state_root_deterministic_and_changes():
    k1, k2 = ChainKernel(), ChainKernel()
    for k in (k1, k2):
        k.apply_block([_cd("mint(bytes20,uint256)", ALICE, 5)])
    assert k1.compute_state_root() == k2.compute_state_root()
    k2.apply_block([_cd("mint(bytes20,uint256)", BOB, 1)])
    assert k1.compute_state_root() != k2.compute_state_root()


def test_block_hash_chains_parent():
    k = ChainKernel()
    b0 = k.apply_block([_cd("mint(bytes20,uint256)", ALICE, 1)])
    b1 = k.apply_block([_cd("mint(bytes20,uint256)", BOB, 1)])
    assert b1.parent_hash == b0.block_hash
    assert b1.number == b0.number + 1


def test_storage_persists_and_indexes(tmp_path):
    db = tmp_path / "t.db"
    k = ChainKernel()
    store = Storage(db)
    cds = [_cd("mint(bytes20,uint256)", ALICE, 100)]
    block = k.apply_block(cds)
    store.save_block(block, cds, k.accounts)

    txh = block.receipts[0].tx_hash
    row = store.get_transaction(txh)
    assert row is not None and row["status"] == "ok"
    assert store.get_block(0)["state_root"].startswith("0x")
    assert store.transactions_for_account("0x" + ALICE.hex())
    assert store.latest_snapshot("0x" + ALICE.hex())["balance"] == "100"
    assert store.stats()["blocks"] == 1
    store.close()


def test_replay_fixture_passes_and_pins_root():
    result = replay(
        "tests/fixtures/chain_fixture.json", ":memory:", log_dir="test-logs"
    )
    assert result["ok"] is True
    assert result["summary"]["reverted"] == 1
    assert len(result["final_state_root"]) == 66  # 0x + 64 hex


def test_replay_detects_tampered_expectation(tmp_path):
    import json
    from pathlib import Path

    src = json.loads(Path("tests/fixtures/chain_fixture.json").read_text())
    src["expectations"]["balances"]["0x" + "aa" * 20] = "999999"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(src))
    with pytest.raises(ReplayError) as ei:
        replay(str(bad), ":memory:", log_dir="test-logs")
    assert ei.value.code == "balance_mismatch"
