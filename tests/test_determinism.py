"""确定性核验：**不同进程**重放相同输入必须得到相同收据。

测试通过 multiprocessing 在全新解释器进程里重新导入并执行，
序列化结果只通过文件交换（收据哈希），杜绝同进程状态泄漏。
同时断言编码层不依赖任何宿主可变源。
"""
from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from teaching_chain import encoding
from teaching_chain.config import PROGRAM_VERSION
from teaching_chain.kernel import ChainState, normalize_transaction
from teaching_chain.vm import assemble, execute


def _worker_execute(code_hex: str, gas: int, storage_items, q: mp.Queue) -> None:
    storage = dict(storage_items)
    r = execute(bytes.fromhex(code_hex), gas, storage=storage, trace=True)
    q.put({
        "ok": r.ok,
        "gas_used": r.gas_used,
        "storage": [[k, v] for k, v in sorted(r.storage.items())],
        "memory_hex": r.memory_hex,
        "return_value": r.return_value,
        "error_category": r.error_category,
        "trace": r.trace,
        "state_root": encoding.hexhash(
            {str(k): r.storage[k] for k in sorted(r.storage)}
        ),
    })


def _run_in_fresh_process(code_hex, gas, storage):
    ctx = mp.get_context("spawn")  # 全新解释器，不继承父进程内存
    q: mp.Queue = ctx.Queue()
    p = ctx.Process(target=_worker_execute, args=(code_hex, gas, sorted(storage.items()), q))
    p.start()
    result = q.get(timeout=30)
    p.join(timeout=30)
    assert p.exitcode == 0
    return result


@pytest.fixture
def mixed_program():
    text = """
PUSH8 100
PUSH8 7
SSTORE
PUSH8 50
PUSH8 8
SSTORE
PUSH8 8
SLOAD
PUSH8 4
MUL
PUSH8 7
SSTORE
PUSH8 23
PUSH8 0
MSTORE
PUSH8 0
MLOAD
STOP
"""
    return bytes.fromhex(assemble(text).hex())


def test_cross_process_execution_identical(mixed_program):
    storage = {7: 1, 99: -12345}
    local = execute(mixed_program, 200_000, storage=dict(storage), trace=True)
    assert local.ok, local.error_category

    remote = _run_in_fresh_process(mixed_program.hex(), 200_000, storage)

    assert remote["ok"] is True
    assert remote["gas_used"] == local.gas_used
    assert remote["return_value"] == local.return_value == 23
    assert remote["memory_hex"] == local.memory_hex
    assert dict(remote["storage"]) == local.storage
    assert remote["state_root"] == encoding.hexhash(
        {str(k): local.storage[k] for k in sorted(local.storage)}
    )
    assert remote["trace"] == local.trace


def test_cross_process_failure_receipt_identical():
    # 写后除零：必须跨进程得到同一失败类别、同一 gas 消耗与同一回滚状态
    text = """
PUSH8 100
PUSH8 7
SSTORE
PUSH8 1
PUSH8 0
DIV
STOP
"""
    code = bytes.fromhex(assemble(text).hex())
    local = execute(code, 50_000, storage={3: 9}, trace=True)
    remote = _run_in_fresh_process(code.hex(), 50_000, {3: 9})

    assert local.ok is False
    for key in ("ok", "gas_used", "error_category", "state_root"):
        if key == "state_root":
            assert remote[key] == encoding.hexhash({"3": 9})
        else:
            assert remote[key] == getattr(local, key) if key != "ok" else remote[key] is False
    assert dict(remote["storage"]) == {3: 9}


def _worker_block(tx_dicts, db_unused, q, tmp_path_str):
    # 独立进程内封块并输出每笔收据哈希
    state = ChainState()
    block, processed = state.apply_block(tx_dicts)
    q.put({
        "block_hash": block.hash(),
        "state_root": block.state_root,
        "receipt_hashes": [p.receipt.digest() for p in processed],
        "receipts": [p.receipt.to_dict() for p in processed],
    })


def test_cross_process_same_signed_transactions_same_receipts(alice, bob, tmp_path):
    from .conftest import make_tx

    txs = []
    txs.append(make_tx(
        alice,
        assemble("PUSH8 11\nPUSH8 1\nSSTORE\nPUSH8 1\nSLOAD\nSTOP").hex(),
        nonce=1,
    ))
    txs.append(make_tx(
        bob,
        assemble(
            "PUSH8 1\nPUSH8 0\nDIV\nSTOP"
        ).hex(),
        nonce=2,
    ))
    txs.append(make_tx(
        alice,
        assemble("PUSH8 22\nPUSH8 2\nSSTORE\nSTOP").hex(),
        nonce=3,
    ))

    # 进程 1（主进程）
    state1 = ChainState()
    block1, processed1 = state1.apply_block(json.loads(json.dumps(txs)))

    # 进程 2（全新 spawn）
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    p = ctx.Process(
        target=_worker_block,
        args=(json.loads(json.dumps(txs)), None, q, str(tmp_path)),
    )
    p.start()
    remote = q.get(timeout=30)
    p.join(timeout=30)
    assert p.exitcode == 0

    assert remote["block_hash"] == block1.hash()
    assert remote["state_root"] == block1.state_root
    assert remote["receipt_hashes"] == [r.receipt.digest() for r in processed1]

    # 逐字段断言失败交易也被复现
    failed_local = processed1[1].receipt
    failed_remote = remote["receipts"][1]
    assert failed_local.status == 0
    assert failed_remote["status"] == 0
    assert failed_remote["error_category"] == failed_local.error_category == "DIV_BY_ZERO"
    assert failed_remote["gas_used"] == failed_local.gas_used
    assert failed_remote["state_root"] == failed_local.state_root


def test_receipt_binds_program_version_and_input_digest(alice):
    from .conftest import make_tx

    code_hex = assemble("PUSH8 1\nPUSH8 2\nADD\nSTOP").hex()
    tx = make_tx(alice, code_hex, nonce=7)
    state = ChainState()
    _, processed = state.apply_block([tx])
    receipt = processed[0].receipt
    assert receipt.program_version == PROGRAM_VERSION
    assert receipt.receipt_version >= 1
    # input_digest 是对被签内容（无签名）的哈希；改签名不应改它
    assert receipt.input_digest == encoding.transaction_digest({
        "chain": tx["chain"], "nonce": tx["nonce"],
        "code": tx["code"], "gas_limit": tx["gas_limit"],
        "pubkey": tx["pubkey"],
    })
    # tx_hash 覆盖含签名的完整交易
    assert receipt.tx_hash != receipt.input_digest
    assert len(bytes.fromhex(receipt.tx_hash)) == 32


def test_encoding_is_canonical_and_sorted():
    a = {"z": 1, "a": [True, False, None, -3, "大"], "m": {"x": b"\x00\x01"}}
    b = {"m": {"x": b"\x00\x01"}, "a": [True, False, None, -3, "大"], "z": 1}
    assert encoding.encode(a) == encoding.encode(b)
    assert encoding.decode(encoding.encode(a)) == a


@pytest.mark.parametrize("value", [0, 1, -1, 2**63 - 1, -(2**63), 255, 256])
def test_encoding_int_roundtrip(value):
    assert encoding.decode(encoding.encode(value)) == value


def test_encoding_rejects_out_of_range_int():
    with pytest.raises(encoding.EncodingError):
        encoding.encode(2**64)
    with pytest.raises(encoding.EncodingError):
        encoding.encode(-(2**63) - 1)


def test_no_host_time_or_random_in_execution(mixed_program):
    # 同样输入连续执行多次结果完全一致（若 VM 触碰随机/时间源会不稳定）
    results = [
        execute(mixed_program, 200_000, {7: 1}, trace=True)
        for _ in range(5)
    ]
    digests = {
        (r.gas_used, r.memory_hex,
         encoding.hexhash({str(k): v for k, v in r.storage.items()}),
         tuple(r.trace))
        for r in results
    }
    assert len(digests) == 1
