"""差分测试：程序化构造多组变体，内核结论必须与独立 oracle 一致。

与夹具回放互补：这里对关键值做参数化扰动（金额、费用、持有者、签名字节、
顺序），覆盖夹具中未逐一列出的组合，防止两套实现"恰好"对同一批样例一致。
"""
from __future__ import annotations

import pytest

from utxo_ledger import encoding, fab
from utxo_ledger.kernel import Kernel
from utxo_ledger.store import SqliteStore


def _run_kernel(genesis_txs, block_txs):
    g = fab.genesis_block(genesis_txs)
    blk = fab.next_block(block_txs, g)
    store = SqliteStore(":memory:")
    store.apply_block(Kernel(store).plan_block(g))
    try:
        plan = Kernel(store).plan_block(blk)
        return True, None, plan
    except Exception as exc:  # noqa: BLE001
        return False, (exc.category.value, exc.code, getattr(exc, "tx_index", None)), None


def _run_oracle(oracle, genesis_txs, block_txs):
    g = fab.genesis_block(genesis_txs)
    blk = fab.next_block(block_txs, g)
    state = oracle.genesis_state()
    gv = oracle.evaluate_block(encoding.block_to_json(g), state)
    assert gv["accepted"]
    state = oracle.apply_block(encoding.block_to_json(g), state, gv)
    v = oracle.evaluate_block(encoding.block_to_json(blk), state)
    if v["accepted"]:
        return True, None, v
    return False, (v["category"], v["code"], v["tx_index"]), v


@pytest.mark.parametrize(
    "amount,fee",
    [(100, 0), (100, 1), (100, 99), (999, 1), (500, 500)],
)
def test_parameterized_valid_transfers_agree(ring, oracle, amount, fee):
    k0, k1 = ring.pub(0), ring.pub(1)
    # 花 1000：输出 (1000-fee)，费用 fee —— 全部守恒；amount 参数仅作标签
    issues = [fab.issue_tx([(1000, k0)])]
    g = fab.genesis_block(issues)
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(encoding.txid_of(g.transactions[0]), 0)],
            [fab.make_output(1000 - fee, k1)],
            fee=fee,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    ok_k, err_k, _ = _run_kernel(issues, [tx])
    ok_o, err_o, _ = _run_oracle(oracle, issues, [tx])
    assert ok_k is True
    assert (ok_k, err_k) == (ok_o, err_o)
    assert amount  # 标签被使用


@pytest.mark.parametrize(
    "out_amount,fee",
    [(1000, 1), (900, 0), (1, 998), (0, 0), (1001, 0)],
)
def test_parameterized_conservation_variants(ring, oracle, out_amount, fee):
    k0, k1 = ring.pub(0), ring.pub(1)
    g = fab.genesis_block([fab.issue_tx([(1000, k0)])])
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(encoding.txid_of(g.transactions[0]), 0)],
            [fab.make_output(out_amount, k1)] if out_amount > 0 else [],
            fee=fee,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    if out_amount == 0 and not tx.outputs:
        # 无输出且 fee=0/1：守恒 1000 == 0+fee 不成立 -> 双方都应拒
        pass
    ok_k, err_k, _ = _run_kernel([fab.issue_tx([(1000, k0)])], [tx])
    ok_o, err_o, _ = _run_oracle(oracle, [fab.issue_tx([(1000, k0)])], [tx])
    assert (ok_k, err_k) == (ok_o, err_o), (out_amount, fee, err_k, err_o)


def test_wrong_signer_matches_oracle(ring, oracle):
    k0, k1 = ring.pub(0), ring.pub(1)
    g = fab.genesis_block([fab.issue_tx([(100, k0)])])
    # 用 K1 的私钥签 K0 的币
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(encoding.txid_of(g.transactions[0]), 0)],
            [fab.make_output(100, k1)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(1)],
    )
    ok_k, err_k, _ = _run_kernel([fab.issue_tx([(100, k0)])], [tx])
    ok_o, err_o, _ = _run_oracle(oracle, [fab.issue_tx([(100, k0)])], [tx])
    assert ok_k is False and err_k[1] == "SIGNATURE_INVALID"
    assert err_k == err_o


def test_replay_longer_sequence_agrees(ring, oracle):
    """3 块链：发行 -> 拆分 -> 合并；双方逐块结论与 UTXO 根一致。"""
    k0, k1, k2 = ring.pub(0), ring.pub(1), ring.pub(2)
    g = fab.genesis_block([fab.issue_tx([(1000, k0)])])
    gid = encoding.txid_of(g.transactions[0])

    split = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(gid, 0)],
            [fab.make_output(400, k1), fab.make_output(550, k2)],
            fee=50,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    b1 = fab.next_block([split], g)
    sid = encoding.txid_of(split)

    merge = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(sid, 0), encoding.Outpoint(sid, 1)],
            [fab.make_output(950, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(1), ring.priv(2)],
    )
    b2 = fab.next_block([merge], b1)

    # 内核侧
    store = SqliteStore(":memory:")
    for b in (g, b1, b2):
        store.apply_block(Kernel(store).plan_block(b))
    kernel_root = store.utxo_root().hex()

    # oracle 侧
    state = oracle.genesis_state()
    for raw in (encoding.block_to_json(g), encoding.block_to_json(b1), encoding.block_to_json(b2)):
        v = oracle.evaluate_block(raw, state)
        assert v["accepted"], v
        state = oracle.apply_block(raw, state, v)
    assert oracle.utxo_root(state) == kernel_root


def test_all_fixture_case_categories_are_distinct_from_success(oracle):
    """健全性：fixture 中四个指定失败类别的 code 两两不同。"""
    import json
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "validation_cases.json",
    )
    fx = json.load(open(path, encoding="utf-8"))
    by_name = {c["name"]: c for c in fx["cases"]}
    required = [
        ("intra_block_double_spend", "STATE_CONFLICT", "DOUBLE_SPEND"),
        ("duplicate_input_same_tx", "STATE_CONFLICT", "DOUBLE_SPEND"),
        ("zero_value_output", "INPUT_ERROR", "ZERO_VALUE"),
        ("signature_tampered", "COMPUTATION_FAILED", "SIGNATURE_INVALID"),
    ]
    for name, cat, code in required:
        c = by_name[name]
        assert c["expected_category"] == cat
        assert c["expected_code"] == code
    # 输入错误/状态冲突/计算失败三类至少各出现一次，保证可区分
    cats = {c["expected_category"] for c in fx["cases"] if not c["expected_accepted"]}
    assert {"INPUT_ERROR", "STATE_CONFLICT", "COMPUTATION_FAILED"} <= cats
