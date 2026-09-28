"""独立逐交易参考实现 (independent reference oracle).

信任边界：本文件**不得 import utxo_ledger 包**。它按 docs/semantics.md 的
字节布局与规则，用 stdlib（hashlab/struct）+ 仅密码原语（cryptography 做
secp256k1 ECDSA 验证）重新实现一遍。测试夹具生成与差分测试都以它为准，
从而避免"参考答案全部由被测核心自己生成"。

与内核的有意差异（实现风格不同，便于暴露共模错误）：
* 函数式 + dict 状态，不使用领域 dataclass；
* 逐笔交易用"复制再修改"的暂定状态，而非集合增量；
* 编码采用 struct.pack 与手写解析，循环/前向引用判定分开两遍扫描。

输出协议（每个 case 一个 dict）：
{
  "accepted": bool,
  "category": "INPUT_ERROR"|...|None,
  "code": "DOUBLE_SPEND"|...|None,
  "tx_index": int|None,
  "per_tx": [{"index","verdict","sum_in","sum_out","fee"}, ...],
  "utxo_root_after_genesis": "hex"   # 仅 accepted 链序列时给出
}
"""
from __future__ import annotations

import hashlib
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

VERSION = 1
MAX_MONEY = (1 << 63) - 1
HASHN = 32
PUBKN = 33
_ZERO = b"\x00" * HASHN
_ALG = ec.ECDSA(Prehashed(hashes.SHA256()))


# ---------------------------------------------------------------------------
# 编码（对照语义文档重写；与 utxo_ledger.encoding 字节级相同）
# ---------------------------------------------------------------------------
def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes((n,))
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack(">H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack(">I", n)
    return b"\xff" + struct.pack(">Q", n)


def _h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _chain(prefix: bytes, items: list[bytes]) -> bytes:
    acc = _h(prefix)
    for it in items:
        acc = _h(acc + it)
    return acc


def tx_body(tx: dict) -> bytes:
    out = [b"UTXT", struct.pack(">I", tx["version"]), _varint(len(tx["inputs"]))]
    for i in tx["inputs"]:
        out.append(bytes.fromhex(i["txid"]))
        out.append(struct.pack(">I", i["vout"]))
    out.append(_varint(len(tx["outputs"])))
    for o in tx["outputs"]:
        pk = bytes.fromhex(o["pubkey"])
        out.append(struct.pack(">Q", o["amount"]))
        out.append(_varint(len(pk)))
        out.append(pk)
    out.append(struct.pack(">Q", tx["fee"]))
    return b"".join(out)


def tx_full(tx: dict) -> bytes:
    out = [b"UTXW", struct.pack(">I", tx["version"]), _varint(len(tx["inputs"]))]
    for i in tx["inputs"]:
        out.append(bytes.fromhex(i["txid"]))
        out.append(struct.pack(">I", i["vout"]))
    out.append(_varint(len(tx["outputs"])))
    for o in tx["outputs"]:
        pk = bytes.fromhex(o["pubkey"])
        out.append(struct.pack(">Q", o["amount"]))
        out.append(_varint(len(pk)))
        out.append(pk)
    out.append(struct.pack(">Q", tx["fee"]))
    out.append(_varint(len(tx["witnesses"])))
    for w in tx["witnesses"]:
        sig = bytes.fromhex(w["signature"])
        out.append(_varint(len(sig)))
        out.append(sig)
    return b"".join(out)


def txid(tx: dict) -> bytes:
    return _h(tx_body(tx))


def sighash(tx: dict) -> bytes:
    return _h(b"utxo-ledger/sighash/v1" + tx_body(tx))


def header_payload(h: dict) -> bytes:
    return b"".join(
        [
            struct.pack(">I", h["version"]),
            struct.pack(">I", h["height"]),
            bytes.fromhex(h["prev_hash"]),
            struct.pack(">Q", h["timestamp"]),
            bytes.fromhex(h["tx_root"]),
            bytes.fromhex(h["witness_root"]),
        ]
    )


def header_bytes(h: dict) -> bytes:
    return b"UHDR" + header_payload(h)


def block_id(block: dict) -> bytes:
    return _h(header_bytes(block["header"]))


def roots(txs: list[dict]) -> tuple[bytes, bytes]:
    ids = [txid(t) for t in txs]
    return (
        _chain(b"utxo-ledger/txroot/v1", ids),
        _chain(
            b"utxo-ledger/witroot/v1", [_h(tx_full(t)) for t in txs]
        ),
    )


# ---------------------------------------------------------------------------
# 验签（唯一复用外部库的部分）
# ---------------------------------------------------------------------------
def _valid_sig(pubkey_hex: str, sig_hex: str, msg: bytes) -> bool:
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256K1(), bytes.fromhex(pubkey_hex)
        )
        pub.verify(bytes.fromhex(sig_hex), msg, _ALG)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------------------
# 参考状态：dict 形态
#   state = {"tip": (height, block_id_hex) | None,
#            "utxo": {(txid_hex, vout): {"amount","pubkey","height"}},
#            "spent": set((txid_hex, vout))}
# ---------------------------------------------------------------------------
def genesis_state() -> dict:
    return {"tip": None, "utxo": {}, "spent": set()}


def _reject(category: str, code: str, tx_index, message: str, per_tx=None) -> dict:
    return {
        "accepted": False,
        "category": category,
        "code": code,
        "tx_index": tx_index,
        "message": message,
        "per_tx": per_tx or [],
    }


def evaluate_block(
    block: dict,
    state: dict,
    *,
    limits: dict | None = None,
) -> dict:
    """对**单个**块给出判定，不修改 state（成功时调用方再用 apply_block）。"""
    lim = limits or {
        "max_block_bytes": 1_000_000,
        "max_txs_per_block": 10_000,
        "max_inputs_per_tx": 10_000,
        "max_outputs_per_tx": 10_000,
    }
    h = block["header"]
    txs = block["transactions"]
    per_tx: list[dict] = []

    # 链定位
    if state["tip"] is None:
        if h["height"] != 0:
            return _reject("INPUT_ERROR", "BAD_GENESIS", None, "首块必须 height=0")
        if h["prev_hash"] != _ZERO.hex():
            return _reject("INPUT_ERROR", "BAD_GENESIS", None, "genesis prev_hash 必须全零")
        is_genesis = True
    else:
        th, tid = state["tip"]
        if h["height"] == 0:
            return _reject("STATE_CONFLICT", "BLOCK_CONFLICT", None, "genesis 已存在")
        if h["height"] != th + 1:
            return _reject("STATE_CONFLICT", "BLOCK_CONFLICT", None, "高度不连续")
        if h["prev_hash"] != tid:
            return _reject("STATE_CONFLICT", "BLOCK_CONFLICT", None, "prev_hash 不符")
        is_genesis = False

    if not txs:
        return _reject(
            "RESOURCE_EXHAUSTED", "RESOURCE_LIMIT", None, "块必须至少一笔交易"
        )
    if len(txs) > lim["max_txs_per_block"]:
        return _reject("RESOURCE_EXHAUSTED", "RESOURCE_LIMIT", None, "交易数超限")

    ids = [txid(t) for t in txs]
    id_to_idx: dict[bytes, int] = {}
    for i, tid_v in enumerate(ids):
        if tid_v in id_to_idx:
            return _reject(
                "INPUT_ERROR", "TXID_DUPLICATE", i, "块内重复 txid"
            )
        id_to_idx[tid_v] = i

    # 前向引用（先于环）
    for i, t in enumerate(txs):
        for ref in t["inputs"]:
            rtid = bytes.fromhex(ref["txid"])
            if rtid in id_to_idx and id_to_idx[rtid] > i:
                return _reject(
                    "STATE_CONFLICT",
                    "FORWARD_REFERENCE",
                    i,
                    "前向引用",
                    per_tx,
                )
    # 环（DFS 白/灰/黑）
    adj = {i: [] for i in range(len(txs))}
    for i, t in enumerate(txs):
        for ref in t["inputs"]:
            rtid = bytes.fromhex(ref["txid"])
            if rtid in id_to_idx and id_to_idx[rtid] != i:
                adj[id_to_idx[rtid]].append(i)
    color = {i: 0 for i in adj}

    def dfs(n: int) -> bool:
        color[n] = 1
        for m in adj[n]:
            if color[m] == 1:
                return True
            if color[m] == 0 and dfs(m):
                return True
        color[n] = 2
        return False

    if any(color[i] == 0 and dfs(i) for i in adj):
        return _reject(
            "STATE_CONFLICT", "REFERENCE_CYCLE", None, "引用成环", per_tx
        )

    # 暂定状态：以链状态为底（复制），逐笔应用
    live = dict(state["utxo"])  # (hex, vout) -> record
    spent_local: set[tuple[str, int]] = set()
    outputs_local: dict[str, list[dict]] = {}
    for i, t in enumerate(txs):
        outputs_local[ids[i].hex()] = t["outputs"]

    for i, t in enumerate(txs):
        verdict: dict = {"index": i, "txid": ids[i].hex(), "verdict": "planned"}
        if len(t["witnesses"]) != len(t["inputs"]):
            verdict.update(
                verdict="rejected",
                category="INPUT_ERROR",
                code="WITNESS_COUNT_MISMATCH",
            )
            per_tx.append(verdict)
            return _reject(
                "INPUT_ERROR", "WITNESS_COUNT_MISMATCH", i, "见证数量不符", per_tx
            )
        if len(t["inputs"]) > lim["max_inputs_per_tx"] or len(t["outputs"]) > lim[
            "max_outputs_per_tx"
        ]:
            return _reject(
                "RESOURCE_EXHAUSTED", "RESOURCE_LIMIT", i, "输入/输出条数超限", per_tx
            )
        if t["version"] != VERSION:
            return _reject(
                "INPUT_ERROR", "MALFORMED_ENCODING", i, "版本不支持", per_tx
            )
        if not 0 <= t["fee"] <= MAX_MONEY:
            return _reject("INPUT_ERROR", "INVALID_FEE", i, "费用越界", per_tx)
        for v, o in enumerate(t["outputs"]):
            if o["amount"] == 0:
                return _reject("INPUT_ERROR", "ZERO_VALUE", i, "零值输出", per_tx)
            if not 1 <= o["amount"] <= MAX_MONEY or len(o["pubkey"]) != 2 * PUBKN:
                return _reject(
                    "INPUT_ERROR", "AMOUNT_OUT_OF_RANGE", i, "金额/公钥越界", per_tx
                )

        if is_genesis:
            if t["inputs"] or t["fee"] != 0:
                return _reject(
                    "INPUT_ERROR", "ILLEGAL_ISSUE", i, "genesis 发行非法", per_tx
                )
        elif not t["inputs"]:
            return _reject(
                "INPUT_ERROR", "ILLEGAL_ISSUE", i, "禁止非 genesis 发行", per_tx
            )

        seen = set()
        for ref in t["inputs"]:
            key = (ref["txid"], ref["vout"])
            if key in seen:
                return _reject(
                    "STATE_CONFLICT", "DOUBLE_SPEND", i, "同交易重复输入", per_tx
                )
            seen.add(key)

        sum_in = 0
        msg = sighash(t)
        for k, ref in enumerate(t["inputs"]):
            key = (ref["txid"], ref["vout"])
            # 暂定花费集合同时覆盖块内与链上输出（提交尚未落库）
            if key in spent_local:
                return _reject(
                    "STATE_CONFLICT", "DOUBLE_SPEND", i, "输出已被本块前序交易花费", per_tx
                )
            if key in state["spent"]:
                return _reject(
                    "STATE_CONFLICT", "DOUBLE_SPEND", i, "输出已在历史链上花费", per_tx
                )
            if ref["txid"] in outputs_local:
                outs = outputs_local[ref["txid"]]
                if ref["vout"] >= len(outs):
                    return _reject(
                        "STATE_CONFLICT", "UNKNOWN_OUTPOINT", i, "vout 越界", per_tx
                    )
                owner = outs[ref["vout"]]["pubkey"]
                amount = outs[ref["vout"]]["amount"]
            elif key in live:
                rec = live[key]
                owner, amount = rec["pubkey"], rec["amount"]
            else:
                return _reject(
                    "STATE_CONFLICT", "UNKNOWN_OUTPOINT", i, "outpoint 不存在", per_tx
                )
            if not _valid_sig(owner, t["witnesses"][k]["signature"], msg):
                verdict.update(
                    verdict="rejected",
                    category="COMPUTATION_FAILED",
                    code="SIGNATURE_INVALID",
                )
                per_tx.append(verdict)
                return _reject(
                    "COMPUTATION_FAILED",
                    "SIGNATURE_INVALID",
                    i,
                    "签名验证失败",
                    per_tx,
                )
            if sum_in > MAX_MONEY - amount:
                return _reject(
                    "INPUT_ERROR", "AMOUNT_OVERFLOW", i, "求和溢出", per_tx
                )
            sum_in += amount

        sum_out = 0
        for o in t["outputs"]:
            if sum_out > MAX_MONEY - o["amount"]:
                return _reject(
                    "INPUT_ERROR", "AMOUNT_OVERFLOW", i, "求和溢出", per_tx
                )
            sum_out += o["amount"]
        if sum_in < t["fee"]:
            return _reject("INPUT_ERROR", "INVALID_FEE", i, "费用超输入", per_tx)
        # 常规交易守恒；genesis 发行交易无输入，价值由"仅 genesis 可发行"规则约束
        if not is_genesis and sum_in != sum_out + t["fee"]:
            return _reject(
                "INPUT_ERROR",
                "CONSERVATION_MISMATCH",
                i,
                "价值不守恒",
                per_tx,
            )

        # 暂定应用
        for ref in t["inputs"]:
            key = (ref["txid"], ref["vout"])
            spent_local.add(key)
            live.pop(key, None)
        for v, o in enumerate(t["outputs"]):
            live[(ids[i].hex(), v)] = {
                "amount": o["amount"],
                "pubkey": o["pubkey"],
                "height": h["height"],
            }
        verdict.update(sum_in=sum_in, sum_out=sum_out, fee=t["fee"])
        per_tx.append(verdict)

    # 根校验
    calc_txr, calc_wr = roots(txs)
    if calc_txr.hex() != h["tx_root"]:
        return _reject(
            "COMPUTATION_FAILED", "ROOT_MISMATCH", None, "tx_root 不符", per_tx
        )
    if calc_wr.hex() != h["witness_root"]:
        return _reject(
            "COMPUTATION_FAILED", "ROOT_MISMATCH", None, "witness_root 不符", per_tx
        )

    return {
        "accepted": True,
        "category": None,
        "code": None,
        "tx_index": None,
        "per_tx": per_tx,
        "block_id": block_id(block).hex(),
    }


def apply_block(block: dict, state: dict, verdict: dict) -> dict:
    """根据已接受判定推进参考状态，返回新 state（不修改原 state）。"""
    new_state = {
        "tip": (block["header"]["height"], verdict["block_id"]),
        "utxo": dict(state["utxo"]),
        "spent": set(state["spent"]),
    }
    for i, t in enumerate(block["transactions"]):
        tid = txid(t).hex()
        for ref in t["inputs"]:
            key = (ref["txid"], ref["vout"])
            new_state["spent"].add(key)
            new_state["utxo"].pop(key, None)
        for v, o in enumerate(t["outputs"]):
            new_state["utxo"][(tid, v)] = {
                "amount": o["amount"],
                "pubkey": o["pubkey"],
                "height": block["header"]["height"],
            }
    return new_state


def utxo_root(state: dict) -> str:
    rows = []
    for (tid_hex, vout), rec in state["utxo"].items():
        rows.append(
            bytes.fromhex(tid_hex)
            + struct.pack(">I", vout)
            + struct.pack(">Q", rec["amount"])
            + bytes.fromhex(rec["pubkey"])
        )
    rows.sort()
    return _chain(b"utxo-ledger/utxoroot/v1", rows).hex()
