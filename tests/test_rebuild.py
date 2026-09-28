"""三类独立一致性核对：

1. 夹具场景下，被测内核的链/余额/拒绝类别 == 独立 reference 预言机的期望文件；
2. 在线增量派生索引 == 用存储原始块从空库全量重建（rebuild）的结果；
3. 随机化合成链上，内核对每个区块提交后的状态都与 reference 逐步重放一致。

期望不是被测内核生成的：fixtures/*.expected.json 由 reference.replay 离线产出，
test_oracle_matches_fixtures 只读取并比较。
"""

import json
import random

import pytest

from reorgindex import crypto, reference
from reorgindex.errors import (
    DuplicateBlockError,
    FinalityReorgError,
    IndexError as DomainError,
)
from reorgindex.config import Settings
from reorgindex.fixturegen import (
    ALICE, ALICE_PRIV, ALICE_PUB, BOB, BOB_PRIV, BOB_PUB, CAROL,
    PROPOSER_PRIV, PROPOSER_PUB, block, tx,
)
from reorgindex.kernel import ChainKernel
from reorgindex.replay import rebuild_in_memory, replay_jsonl, verify_against_rebuild
from reorgindex.storage import Storage

from .conftest import FINALITY_DEPTH, load_expected, load_fixture

def _ingest_all(kernel, blocks, request_id="t"):
    """提交全部区块，返回内核实际给出的拒绝类别列表（挂起不算拒绝）。"""

    cats = []
    for b in blocks:
        try:
            kernel.ingest(b, request_id=request_id)
        except DomainError as exc:
            cats.append(exc.category)
    return cats


SCENARIOS = [
    "short_fork_wins",
    "deep_fork_rejected",
    "duplicate_tx_across_fork",
    "pending_drain",
]


def _fresh_kernel(depth=FINALITY_DEPTH):
    storage = Storage(":memory:")
    kernel = ChainKernel(storage, Settings(db_path=":memory:", finality_depth=depth))
    return storage, kernel


@pytest.mark.parametrize("name", SCENARIOS)
def test_kernel_matches_independent_oracle_fixtures(name):
    storage, kernel = _fresh_kernel()
    blocks = load_fixture(name)
    expected = load_expected(name)

    _ingest_all(kernel, blocks, request_id=f"fixture-{name}")

    assert kernel.tip_hash() == expected["tip_hash"]
    assert kernel.canonical_chain() == expected["canonical_chain"]
    assert kernel.all_balances() == expected["balances"]
    assert storage.contribution_count() == expected["contribution_count"]
    # 拒绝类别精确匹配（如深分叉必须是 finality_reorg）。挂起在预言机里也进
    # rejected 流，但内核将其表达为 block_pending（不是拒绝），这里剔除它。
    actual_rejects = [e["payload"].get("reason")
                      for e in storage.recent_diag(500)
                      if e["event"] == "block_rejected"]
    expected_cats = [r["category"] for r in expected["rejected"]
                     if r["category"] != "unknown_parent"]
    assert sorted(filter(None, actual_rejects)) == sorted(expected_cats)
    storage.close()


@pytest.mark.parametrize("name", SCENARIOS)
def test_online_derived_index_matches_full_rebuild(name, tmp_path):
    """当前最佳链派生结果与从存储全量重建逐项一致。"""

    db = tmp_path / f"{name}.sqlite3"
    storage = Storage(str(db))
    kernel = ChainKernel(storage, Settings(db_path=str(db), finality_depth=FINALITY_DEPTH))
    _ingest_all(kernel, load_fixture(name))

    result = verify_against_rebuild(kernel)
    assert result["ok"] is True, f"重建不一致: {result['mismatches']}"
    assert result["current"]["balances"] == result["rebuilt"]["balances"]
    assert result["current"]["canonical_chain"] == result["rebuilt"]["canonical_chain"]
    storage.close()


def test_replay_jsonl_and_rebuild_cli_paths(tmp_path):
    storage, kernel = _fresh_kernel()
    report = replay_jsonl(kernel, "fixtures/short_fork_wins.jsonl", request_id="cli-replay")
    assert report.ingested == 5
    assert report.switched == 1
    # 离线回放后做重建核对
    check = verify_against_rebuild(kernel)
    assert check["ok"]
    # 回滚区间被审计记录：撤 a2（高2）加 f2（高2）
    reorg = report.reorgs[0]
    assert len(reorg["disconnected"]) == 1
    assert len(reorg["connected"]) == 1
    assert reorg["rollback_from_height"] == 2
    assert reorg["rollback_to_height"] == 2
    storage.close()


def test_rebuild_from_persisted_db_survives_reopen(tmp_path):
    db = tmp_path / "persist.sqlite3"
    storage = Storage(str(db))
    kernel = ChainKernel(storage, Settings(db_path=str(db), finality_depth=FINALITY_DEPTH))
    _ingest_all(kernel, load_fixture("duplicate_tx_across_fork"))
    before = {"tip": kernel.tip_hash(), "balances": kernel.all_balances(),
              "chain": kernel.canonical_chain()}
    storage.close()

    # 重新打开：派生索引仍在，且与重建一致
    storage2 = Storage(str(db))
    kernel2 = ChainKernel(storage2, Settings(db_path=str(db), finality_depth=FINALITY_DEPTH))
    assert kernel2.tip_hash() == before["tip"]
    assert kernel2.all_balances() == before["balances"]
    assert verify_against_rebuild(kernel2)["ok"]
    storage2.close()


# --------------------------------------------------------------- 随机化交叉验证

def _random_scenario(seed):
    """生成一棵随机区块树：高度、权重、随机交易；以随机顺序提交。"""

    rng = random.Random(seed)
    g = block(crypto.ZERO_HASH, 0, [], 1)
    levels = [[g]]
    blocks = [g]
    nonces = {"a": 0, "b": 0}
    for height in range(1, 10):
        parents = levels[-1]
        n_children = rng.randint(1, 3)
        children = []
        for _ in range(n_children):
            parent = rng.choice(parents)
            txs = []
            for _ in range(rng.randint(0, 2)):
                who = rng.choice(["a", "b"])
                if who == "a":
                    t = tx(ALICE_PRIV, ALICE_PUB, rng.choice([ALICE, BOB, CAROL]),
                           rng.randint(1, 9), nonces["a"])
                    nonces["a"] += 1
                else:
                    t = tx(BOB_PRIV, BOB_PUB, rng.choice([ALICE, BOB, CAROL]),
                           rng.randint(1, 9), nonces["b"])
                    nonces["b"] += 1
                txs.append(t)
            children.append(block(parent["header"]["block_hash"], height, txs, rng.randint(1, 4)))
        levels.append(children)
        blocks.extend(children)
    rng.shuffle(blocks)
    return blocks


@pytest.mark.parametrize("seed", range(12))
def test_randomized_kernel_matches_oracle_and_rebuild(seed):
    blocks = _random_scenario(seed)
    storage, kernel = _fresh_kernel(depth=4)
    cats = _ingest_all(kernel, blocks, request_id=f"rand-{seed}")

    ref_state = reference.replay(blocks, finality_depth=4)
    ref_view = reference.derived_view(ref_state)

    assert kernel.canonical_chain() == ref_view["canonical_chain"]
    assert kernel.tip_hash() == ref_view["tip_hash"]
    assert kernel.all_balances() == ref_view["balances"]
    assert sorted(kernel.storage.contributing_block_hashes()) == ref_view["contributing_blocks"]

    # 最终性拒绝次数也必须一致
    assert cats.count("finality_reorg") == len(ref_state.finality_rejects)
    # 重复块拒绝次数一致（随机树上可能出现同体同哈希块）
    ref_dupes = [r for r in ref_state.rejected if r.category == "duplicate_block"]
    assert cats.count("duplicate_block") == len(ref_dupes)

    check = verify_against_rebuild(kernel)
    assert check["ok"], f"seed {seed} 重建不一致: {check['mismatches']}"
    storage.close()
