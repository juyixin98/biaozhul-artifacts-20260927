"""链状态内核：签名校验、收据字段、链号、查重、费用/状态回滚在交易层的表现。"""
from __future__ import annotations

import pytest

from teaching_chain import encoding
from teaching_chain.kernel import (
    GENESIS_PARENT,
    ChainState,
    TransactionError,
    normalize_transaction,
    process_transaction,
    state_root,
)
from teaching_chain.vm import assemble

from .conftest import asm, make_tx


def test_valid_transaction_produces_success_receipt(alice):
    tx = make_tx(alice, asm("PUSH8 42\nPUSH8 1\nSSTORE\nSTOP"), nonce=1)
    state = ChainState()
    block, processed = state.apply_block([tx])
    assert block.number == 0
    assert block.parent_hash == GENESIS_PARENT
    r = processed[0].receipt
    assert r.status == 1
    assert r.gas_used == r.intrinsic_gas + 26  # PUSH,PUSH,SSTORE,STOP
    assert r.return_value == 0
    assert block.state_root == state_root({1: 42})
    # 区块哈希由全部收据 + 状态根决定
    assert len(bytes.fromhex(block.hash())) == 32


def test_intrinsic_insufficient_gas_is_status_zero_with_fee_kept(alice):
    # 一大段非零字节码 + 极小 gas_limit
    code_hex = "01" * 10
    tx = make_tx(alice, code_hex, gas_limit=30, nonce=2)
    state = ChainState()
    block, processed = state.apply_block([tx])
    r = processed[0].receipt
    assert r.status == 0
    assert r.error_category == "OUT_OF_GAS"
    assert r.gas_used == 30              # 保留全部预算
    assert r.intrinsic_gas == 21 + 40
    assert state.storage == {}


def test_bad_signature_rejected_before_block(alice):
    tx = make_tx(alice, asm("STOP"), nonce=3)
    tx["signature"] = "00" * 64
    state = ChainState()
    with pytest.raises(TransactionError) as exc:
        state.apply_block([tx])
    assert exc.value.code == "TX_BAD_SIGNATURE"
    assert state.height == -1           # 未封任何块


def test_signer_not_matching_pubkey_rejected(alice, bob):
    tx = make_tx(alice, asm("STOP"), nonce=4)
    tx["signer"] = bob.address_hex()    # 用 alice 的签名却声称 bob
    state = ChainState()
    with pytest.raises(TransactionError) as exc:
        state.apply_block([tx])
    assert exc.value.code == "TX_BAD_SIGNATURE"


def test_wrong_chain_rejected(alice):
    tx = make_tx(alice, asm("STOP"), chain="other-chain", nonce=5)
    state = ChainState()
    with pytest.raises(TransactionError) as exc:
        state.apply_block([tx])
    assert exc.value.code == "TX_CHAIN_MISMATCH"


def test_duplicate_transaction_rejected(alice):
    tx = make_tx(alice, asm("PUSH8 1\nPOP\nSTOP"), nonce=6)
    state = ChainState()
    state.apply_block([tx])
    with pytest.raises(TransactionError) as exc:
        state.apply_block([tx])
    assert exc.value.code == "TX_DUPLICATE"


def test_modified_code_after_signing_rejected(alice):
    tx = make_tx(alice, asm("PUSH8 1\nPOP\nSTOP"), nonce=7)
    tx["code"] = asm("PUSH8 2\nPOP\nSTOP")  # 签名后篡改字节码
    state = ChainState()
    with pytest.raises(TransactionError) as exc:
        state.apply_block([tx])
    assert exc.value.code == "TX_BAD_SIGNATURE"


def test_batch_is_atomic_on_static_failure(alice):
    good = make_tx(alice, asm("PUSH8 5\nPUSH8 1\nSSTORE\nSTOP"), nonce=8)
    bad = make_tx(alice, asm("STOP"), nonce=9)
    bad["code"] = "ff"
    state = ChainState()
    with pytest.raises(TransactionError):
        state.apply_block([good, bad])
    assert state.height == -1
    assert state.storage == {}


def test_execution_failure_still_committed_but_state_rolled_back(alice):
    good = make_tx(alice, asm("PUSH8 5\nPUSH8 1\nSSTORE\nSTOP"), nonce=10)
    bad = make_tx(alice, asm("PUSH8 9\nPUSH8 2\nSSTORE\nPUSH8 1\nPUSH8 0\nDIV\nSTOP"), nonce=11)
    after = make_tx(alice, asm("PUSH8 7\nPUSH8 3\nSSTORE\nSTOP"), nonce=12)
    state = ChainState()
    block, processed = state.apply_block([good, bad, after])
    assert [p.receipt.status for p in processed] == [1, 0, 1]
    # 第二笔失败回滚其自身写入；第一、三笔保留
    assert state.storage == {1: 5, 3: 7}
    failed = processed[1].receipt
    assert failed.error_category == "DIV_BY_ZERO"
    assert failed.gas_used == failed.gas_limit  # 全耗
    # 收据状态根是“回滚后”的状态：只有第一笔效果
    assert failed.state_root == state_root({1: 5})


def test_blocks_chain_by_hashes(alice):
    state = ChainState()
    tx1 = make_tx(alice, asm("STOP"), nonce=20)
    b1, _ = state.apply_block([tx1])
    tx2 = make_tx(alice, asm("STOP"), nonce=21)
    b2, _ = state.apply_block([tx2])
    assert b1.number == 0 and b2.number == 1
    assert b2.parent_hash == b1.hash()
    assert state.head_hash == b2.hash()


def test_receipt_digest_changes_with_version(alice, monkeypatch):
    import teaching_chain.kernel as kernel

    tx = make_tx(alice, asm("STOP"), nonce=30)
    normalized = normalize_transaction(tx)
    p1 = process_transaction(normalized, {}, "teaching-chain-local", 0, 0)
    monkeypatch.setattr(kernel, "PROGRAM_VERSION", "teaching-chain-vm/9.9.9")
    p2 = process_transaction(normalized, {}, "teaching-chain-local", 0, 0)
    # 语义结果相同，但程序版本绑定使收据摘要不同
    assert p1.receipt.status == p2.receipt.status == 1
    assert p1.receipt.digest() != p2.receipt.digest()


def test_dry_run_does_not_mutate_state(alice):
    state = ChainState()
    tx = make_tx(alice, asm("PUSH8 5\nPUSH8 1\nSSTORE\nSTOP"), nonce=40)
    p = state.dry_run(tx)
    assert p.receipt.status == 1
    assert state.height == -1
    assert state.storage == {}
    assert state.head_hash == GENESIS_PARENT


def test_input_digest_covers_only_signed_payload(alice):
    tx1 = make_tx(alice, asm("STOP"), nonce=50, gas_limit=500)
    # 同样的被签内容（另一合法签名者不影响 input_digest？链/nonce/code/gas 相同）
    p = process_transaction(normalize_transaction(tx1), {}, "teaching-chain-local", 0, 0)
    unsigned = {
        "chain": tx1["chain"], "nonce": tx1["nonce"],
        "code": tx1["code"], "gas_limit": tx1["gas_limit"],
        "pubkey": tx1["pubkey"],
    }
    assert p.receipt.input_digest == encoding.transaction_digest(unsigned)
