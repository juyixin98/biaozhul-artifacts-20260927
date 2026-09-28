"""负向测试：每类攻击断言具体 code/category/tx_index，并验证失败后 UTXO 原样。

重点四场景（需求指定）：块内双花、重复输入、零值输出、签名篡改。
另覆盖历史双花、未知 outpoint、前向引用、守恒、非法发行、费用溢出、
资源耗尽（RESOURCE_EXHAUSTED 必须与前几类可区分）。
"""
from __future__ import annotations

import pytest

from utxo_ledger import encoding, fab
from utxo_ledger.errors import (
    AmountOverflowError,
    ConservationMismatchError,
    DoubleSpendError,
    ErrorCategory,
    ForwardReferenceError,
    IllegalIssuanceError,
    ResourceLimitError,
    SignatureError,
    UnknownOutpointError,
    WitnessCountMismatchError,
    ZeroValueError,
)
from utxo_ledger.journal import assert_state_unchanged, snapshot
from utxo_ledger.kernel import Kernel, Limits
from utxo_ledger.store import SqliteStore


def _try_block(store, block):
    """规划（成功才提交），返回 (error, before, after)。失败时绝不写存储。"""
    before = snapshot(store, "before")
    try:
        plan = Kernel(store).plan_block(block)
    except Exception as exc:  # noqa: BLE001 - 测试需要捕获并断言类别
        after = snapshot(store, "after")
        return exc, before, after
    store.apply_block(plan)
    return None, before, snapshot(store, "after")


# ---------------------------------------------------------------------------
# 需求指定的四大场景
# ---------------------------------------------------------------------------
def test_intra_block_double_spend_rejected_and_state_preserved(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    first = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    second = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k1"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    err, before, after = _try_block(store, fab.next_block([first, second], b1))
    assert isinstance(err, DoubleSpendError)
    assert err.category is ErrorCategory.STATE_CONFLICT
    assert err.code == "DOUBLE_SPEND"
    assert err.tx_index == 1
    assert err.details["txid"] == b1_ids[0].hex()
    assert_state_unchanged(before, after)
    # 第一笔也不得落地（整块不提交）
    assert store.classify_outpoint(b1_ids[0], 0) == "unspent"


def test_duplicate_input_within_same_tx_rejected(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0), encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(1800, pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2), ring.priv(2)],
    )
    err, before, after = _try_block(store, fab.next_block([tx], b1))
    assert isinstance(err, DoubleSpendError)
    assert err.category is ErrorCategory.STATE_CONFLICT
    assert err.tx_index == 0
    assert_state_unchanged(before, after)


def test_zero_value_output_rejected_before_signature(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    # 即使签名完全正确，零值也必须先被拒（INPUT_ERROR，与验签失败可区分）
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k0"]), fab.make_output(0, pubs["k1"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    err, before, after = _try_block(store, fab.next_block([tx], b1))
    assert isinstance(err, ZeroValueError)
    assert err.category is ErrorCategory.INPUT_ERROR
    assert err.details == {"vout": 1}
    assert_state_unchanged(before, after)


def test_tampered_signature_rejected_and_classified(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    good = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k1"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    sig = bytearray(good.witnesses[0].signature)
    sig[-1] ^= 0x01
    bad = fab.with_witnesses(good, [bytes(sig)])
    err, before, after = _try_block(store, fab.next_block([bad], b1))
    assert isinstance(err, SignatureError)
    assert err.category is ErrorCategory.COMPUTATION_FAILED
    assert err.code == "SIGNATURE_INVALID"
    assert_state_unchanged(before, after)


# ---------------------------------------------------------------------------
# 其余冲突/输入错误
# ---------------------------------------------------------------------------
def test_historical_double_spend(base_chain):
    store, g, b1, pubs, ring = base_chain
    g_ids = [encoding.txid_of(t) for t in g.transactions]
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_ids[0], 0)],  # g:0 已在 b1 被 t1 花掉
            [fab.make_output(1000, pubs["k1"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    err, _, after_before = _try_block(store, fab.next_block([tx], b1))
    assert isinstance(err, DoubleSpendError)
    assert err.category is ErrorCategory.STATE_CONFLICT


def test_unknown_outpoint(base_chain):
    store, g, b1, pubs, ring = base_chain
    # 独特金额 777 使交易体区别于底座中任何交易
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b"\x44" * 32, 7)],
            [fab.make_output(777, pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    err, _, _ = _try_block(store, fab.next_block([tx], b1))
    assert isinstance(err, UnknownOutpointError)
    assert err.category is ErrorCategory.STATE_CONFLICT
    assert err.details["vout"] == 7


def test_forward_reference_rejected(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    later = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    earlier = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[1], 0),
             encoding.Outpoint(encoding.txid_of(later), 0)],
            [fab.make_output(500, pubs["k1"]), fab.make_output(900, pubs["k2"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0), ring.priv(2)],
    )
    err, before, after = _try_block(store, fab.next_block([earlier, later], b1))
    assert isinstance(err, ForwardReferenceError)
    assert err.category is ErrorCategory.STATE_CONFLICT
    assert err.tx_index == 0
    assert_state_unchanged(before, after)


def test_conservation_mismatch(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],  # 900
            [fab.make_output(800, pubs["k0"])],  # 输出 800 费 0 -> 差 100
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    err, _, _ = _try_block(store, fab.next_block([tx], b1))
    assert isinstance(err, ConservationMismatchError)
    assert err.category is ErrorCategory.INPUT_ERROR
    assert err.details["difference"] == 100


def test_fee_exceeds_inputs(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(800, pubs["k0"])],
            fee=100,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    # 800+100=900 合法；改为 fee=101 即费用超输入守恒失败
    tx2 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(800, pubs["k0"])],
            fee=101,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    err, _, _ = _try_block(store, fab.next_block([tx2], b1))
    assert isinstance(err, ConservationMismatchError)
    # fee==900 且输出 0 不允许（零输出列表），单独验证 fee > sum_in：
    tx3 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(1, pubs["k0"])],
            fee=899,  # 1+899=900 合法
        ),
        owner_privkeys=[ring.priv(2)],
    )
    plan = Kernel(store).plan_block(fab.next_block([tx3], b1))  # 不应抛
    assert plan.results[0].tx.fee == 899
    # 真正 fee > sum_in：输出 1 fee 900
    tx4 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(1, pubs["k0"])],
            fee=900,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    with pytest.raises(ConservationMismatchError):
        Kernel(store).plan_block(fab.next_block([tx4], b1))


def test_illegal_issuance_after_genesis(base_chain):
    store, g, b1, _pubs, _ring = base_chain
    err, _, _ = _try_block(store, fab.next_block([fab.issue_tx([(1, _pubs["k0"])])], b1))
    assert isinstance(err, IllegalIssuanceError)
    assert err.category is ErrorCategory.INPUT_ERROR


def test_witness_count_mismatch(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    good = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    extra = encoding.Transaction(
        version=good.version,
        inputs=good.inputs,
        outputs=good.outputs,
        fee=good.fee,
        witnesses=good.witnesses
        + (encoding.Witness(signature=b"\x30\x06\x02\x01\x01\x02\x01\x01"),),
    )
    err, _, _ = _try_block(store, fab.next_block([extra], b1))
    assert isinstance(err, WitnessCountMismatchError)
    assert err.category is ErrorCategory.INPUT_ERROR


def test_amount_overflow_rejected(ring):
    """输入求和超过 MAX_MONEY：AMOUNT_OVERFLOW，独立类别，块不提交。

    构造：genesis 发两枚 2^62（各 < 2^63-1，输出求和在发行块不做守恒，
    但 sum_out 仍受溢出保护：2*2^62=2^63 会先溢出，因此 genesis 用两笔
    发行交易、各一枚 2^62，规避输出求和）；高度 1 的交易引用两枚，
    输入求和 2^63 > MAX_MONEY -> AMOUNT_OVERFLOW。
    """
    k2, k3 = ring.pub(2), ring.pub(3)
    half = 1 << 62
    # 两笔发行交易体不同（收款方不同），避免 txid 重复；各发一枚 2^62
    g = fab.genesis_block(
        [fab.issue_tx([(half, k2)]), fab.issue_tx([(half, k3)])]
    )
    g_ids = [encoding.txid_of(t) for t in g.transactions]
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [
                encoding.Outpoint(g_ids[0], 0),
                encoding.Outpoint(g_ids[1], 0),
            ],
            [fab.make_output(1, k2)],  # 输出求和仅 1，不先溢出
            fee=0,
        ),
        owner_privkeys=[ring.priv(2), ring.priv(3)],
    )
    store = SqliteStore(":memory:")
    store.apply_block(Kernel(store).plan_block(g))
    with pytest.raises(AmountOverflowError) as ei:
        Kernel(store).plan_block(fab.next_block([tx], g))
    assert ei.value.category is ErrorCategory.INPUT_ERROR
    assert ei.value.code == "AMOUNT_OVERFLOW"
    assert store.utxo_count() == 2  # 坏块未提交，两枚发行 UTXO 原样存活


# ---------------------------------------------------------------------------
# 资源耗尽（必须与上述三类可区分）
# ---------------------------------------------------------------------------
def test_resource_limit_distinct_category(base_chain):
    store, g, b1, _pubs, _ring = base_chain
    # 收紧到每块最多 1 笔，提交 2 笔
    txs = []
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    tx0 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, _pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[_ring.priv(2)],
    )
    with pytest.raises(ResourceLimitError) as ei:
        Kernel(store, limits=Limits(max_txs_per_block=1)).plan_block(
            fab.next_block([tx0, tx0], b1)
        )
    assert ei.value.category is ErrorCategory.RESOURCE_EXHAUSTED
    assert ei.value.details["limit_name"] == "max_txs_per_block"
    assert ei.value.details["value"] == 2


def test_resource_limit_inputs_per_tx(base_chain):
    store, g, b1, pubs, ring = base_chain
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, pubs["k0"])],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    with pytest.raises(ResourceLimitError) as ei:
        Kernel(store, limits=Limits(max_inputs_per_tx=0)).plan_block(
            fab.next_block([tx], b1)
        )
    assert ei.value.category is ErrorCategory.RESOURCE_EXHAUSTED


# ---------------------------------------------------------------------------
# 防御深度：拓扑环检测（线格式不可自然构造，直接调内部检查）
# ---------------------------------------------------------------------------
def test_topology_cycle_detection_unit(base_chain):
    """哈希 txid + 仅后向引用下线格式无法自然产生环（见 docs/semantics.md），
    因此对抽出的纯函数 _find_cycle_nodes 做 Kahn 判定的直接单元覆盖。"""
    from utxo_ledger.kernel import _find_cycle_nodes

    # 无环 DAG：0->1, 0->2, 1->2
    assert _find_cycle_nodes(3, {0: [1, 2], 1: [2], 2: []}) == []
    # 2 环 0->1->0
    assert _find_cycle_nodes(2, {0: [1], 1: [0]}) == [0, 1]
    # 3 环 0->1->2->0：全部节点都在环上
    assert _find_cycle_nodes(3, {0: [1], 1: [2], 2: [0]}) == [0, 1, 2]
    # 环 + 尾部：0->1->0 成环，2 指向 0（2 不在环上）
    assert _find_cycle_nodes(3, {0: [1], 1: [0], 2: [0]}) == [0, 1]


def test_reference_graph_classifies_forward_edges(base_chain):
    """前向边收集对块结构正确（与环分离）。"""
    from utxo_ledger import encoding
    from utxo_ledger.kernel import _build_reference_graph

    store, g, b1, pubs, _ring = base_chain
    id_x, id_y = b"\x01" * 32, b"\x02" * 32

    def t(refs):
        return encoding.Transaction(
            version=1,
            inputs=tuple(encoding.TxInput(encoding.Outpoint(r, 0)) for r in refs),
            outputs=(fab.make_output(1, pubs["k0"]),),
            fee=0,
            witnesses=(),
        )

    blk = encoding.Block(
        header=encoding.BlockHeader(
            version=1,
            height=2,
            prev_hash=encoding.block_id_of(b1),
            timestamp=fab.FIXTURE_TIMESTAMP,
            tx_root=encoding.ZERO_HASH,
            witness_root=encoding.ZERO_HASH,
        ),
        transactions=(t([id_y]), t([id_x]), t([id_x])),
    )
    # id_x 在 0（被 1、2 后向引用），id_y 在 1（被 0 前向引用）
    forward, adj = _build_reference_graph(blk, {id_x: 0, id_y: 1})
    assert len(forward) == 1 and forward[0][0] == 0 and forward[0][1] == 1
    # 邻接表方向 被引用者 j -> 引用者 i：id_x(=0) 被节点 1、2 引用
    assert adj[0] == [1, 2]
    assert adj[1] == []  # id_y 只被前向引用，不入后向邻接
