"""确定性合成夹具生成。

所有密钥从固定种子派生（Ed25519），因此任何人重新生成都会得到逐字节相同的
区块哈希；每个场景的期望结果由独立预言机 reference.replay 计算后落盘，
评审无需信任被测内核即可核对。
"""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import crypto, reference

FINALITY_DEPTH = 6


def _key(seed_byte: int):
    """固定 32 字节种子派生 Ed25519，保证夹具逐字节可复现。"""

    priv = Ed25519PrivateKey.from_private_bytes(bytes([seed_byte]) * 32)
    return priv, priv.public_key().public_bytes_raw().hex()


PROPOSER_PRIV, PROPOSER_PUB = _key(0x11)
ALICE_PRIV, ALICE_PUB = _key(0x22)
BOB_PRIV, BOB_PUB = _key(0x33)
ALICE = crypto.address_of_pubkey(ALICE_PUB)
BOB = crypto.address_of_pubkey(BOB_PUB)
CAROL = "c0" + "ab" * 31  # 合成收款地址（无需对应私钥即可收款）


def tx(sender_priv, sender_pub, recipient: str, amount: int, nonce: int, memo: str = ""):
    body = {"sender_pubkey": sender_pub, "recipient": recipient,
            "amount": amount, "nonce": nonce, "memo": memo}
    body["tx_id"] = crypto.tx_id_of(body)
    body["signature"] = crypto.sign_tx(sender_priv, body)
    return body


def block(prev_hash: str, height: int, txs: list[dict], weight: int):
    header = {
        "version": crypto.BLOCK_VERSION,
        "prev_hash": prev_hash,
        "height": height,
        "merkle_root": crypto.merkle_root([t["tx_id"] for t in txs]),
        "weight": weight,
        "proposer": PROPOSER_PUB if height > 0 else "",
        "signature": "",
    }
    header["block_hash"] = crypto.block_hash_of(header)
    if height > 0:
        header["signature"] = crypto.sign_block(PROPOSER_PRIV, header)
    return {"header": header, "txs": txs}


def _dump(path: Path, blocks: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for b in blocks:
            fh.write(json.dumps(b, sort_keys=True, ensure_ascii=False) + "\n")


def _expected(blocks: list[dict]) -> dict:
    state = reference.replay(blocks, FINALITY_DEPTH)
    view = reference.derived_view(state)
    return {
        "finality_depth": FINALITY_DEPTH,
        "tip_hash": view["tip_hash"],
        "canonical_chain": view["canonical_chain"],
        "balances": view["balances"],
        "contribution_count": view["contribution_count"],
        "contributing_blocks": view["contributing_blocks"],
        "switches": state.switches,
        "rejected": [{"block_hash": r.block_hash, "category": r.category, "detail": r.detail}
                     for r in state.rejected],
        "finality_rejects": state.finality_rejects,
        "pending_remaining": sum(len(v) for v in state.pending.values()),
        "received_order": state.order,
    }


# --------------------------------------------------------------- 场景

def scenario_short_fork_wins() -> list[dict]:
    g = block(crypto.ZERO_HASH, 0, [], 1)
    a1 = block(g["header"]["block_hash"], 1, [tx(ALICE_PRIV, ALICE_PUB, ALICE, 10, 0, "a1")], 1)
    a2 = block(a1["header"]["block_hash"], 2, [tx(ALICE_PRIV, ALICE_PUB, ALICE, 20, 1, "a2")], 1)
    # 分叉块 b2：同高度但权重 3 > 旧段权重 1（仅 a2），短分叉胜出。
    b2 = block(a1["header"]["block_hash"], 2, [tx(ALICE_PRIV, ALICE_PUB, BOB, 5, 2, "b2")], 3)
    b3 = block(b2["header"]["block_hash"], 3, [tx(ALICE_PRIV, ALICE_PUB, BOB, 7, 3, "b3")], 1)
    return [g, a1, a2, b2, b3]


def scenario_deep_fork_rejected() -> list[dict]:
    g = block(crypto.ZERO_HASH, 0, [], 1)
    a1 = block(g["header"]["block_hash"], 1, [tx(ALICE_PRIV, ALICE_PUB, ALICE, 10, 0)], 1)
    chain = [a1]
    for h in range(2, 9):  # 到高度 8
        chain.append(block(chain[-1]["header"]["block_hash"], h,
                           [tx(ALICE_PRIV, ALICE_PUB, ALICE, 1, h, f"a{h}")], 1))
    # 从高度 1 分叉：要回滚高度 2..8 共 7 块 > 最终性深度 6，必须拒绝。
    f2 = block(a1["header"]["block_hash"], 2, [tx(BOB_PRIV, BOB_PUB, BOB, 1000, 0)], 100)
    return [g, *chain, f2]


def scenario_duplicate_tx() -> list[dict]:
    g = block(crypto.ZERO_HASH, 0, [], 1)
    shared = tx(ALICE_PRIV, ALICE_PUB, CAROL, 100, 0, "shared-on-both-branches")
    a1 = block(g["header"]["block_hash"], 1, [shared], 1)
    a2 = block(a1["header"]["block_hash"], 2, [tx(ALICE_PRIV, ALICE_PUB, ALICE, 4, 1)], 1)
    # b2 重复携带同一笔已签名交易 shared；分叉权重更高会切换，但该 tx 不得产生第二个贡献。
    b2 = block(a1["header"]["block_hash"], 2,
               [shared, tx(ALICE_PRIV, ALICE_PUB, BOB, 8, 2)], 3)
    return [g, a1, a2, b2]


def scenario_pending_drain() -> list[dict]:
    g = block(crypto.ZERO_HASH, 0, [], 1)
    a1 = block(g["header"]["block_hash"], 1, [tx(ALICE_PRIV, ALICE_PUB, ALICE, 11, 0)], 1)
    a2 = block(a1["header"]["block_hash"], 2, [tx(ALICE_PRIV, ALICE_PUB, BOB, 13, 1)], 1)
    # 故意乱序：先到的两个块父未知，需挂起；创世到达后一次排空。
    return [a2, a1, g]


SCENARIOS = {
    "short_fork_wins": scenario_short_fork_wins,
    "deep_fork_rejected": scenario_deep_fork_rejected,
    "duplicate_tx_across_fork": scenario_duplicate_tx,
    "pending_drain": scenario_pending_drain,
}


def generate_all(out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    manifest = {"finality_depth": FINALITY_DEPTH, "scenarios": {}}
    for name, builder in SCENARIOS.items():
        blocks = builder()
        jsonl = out / f"{name}.jsonl"
        expected_file = out / f"{name}.expected.json"
        _dump(jsonl, blocks)
        expected_file.write_text(
            json.dumps(_expected(blocks), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written[name] = jsonl
        manifest["scenarios"][name] = {
            "blocks": len(blocks),
            "fixture": jsonl.name,
            "expected": expected_file.name,
        }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return written
