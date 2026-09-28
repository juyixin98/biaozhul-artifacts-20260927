#!/usr/bin/env python3
"""独立参考答案/夹具生成器（开发期工具，不被被测核心 rsv.* 引用）。

关键要求：参考答案不能由被测核心自身生成。因此本脚本：
- 只依赖 Python 标准库 + 成熟密码库 cryptography，绝不 import rsv；
- 自带交易规范化序列化、sighash、脚本推送编码的独立实现；
- 对每笔 bundle 交易用独立的模板级验证逻辑（直接调成熟库验签）推出预期
  失败类别；哈希向量的预期摘要直接来自 hashlib。

失败类别字符串与 rsv.errors 保持一致是 *契约约定*（docs/failure-codes.md），
不是“从被测代码导入”——测试拿它做独立真值对拍。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

NETWORK = "rsv-local"
DOMAIN_A = "rsv-test-domain-v1"
DOMAIN_B = "rsv-other-domain-v2"

# ---- 失败码契约（与 src/rsv/errors.py 约定一致，独立复制） ----------------
INPUT_BAD_TX = "input.malformed_tx"
INPUT_MALFORMED_PUSH = "input.malformed_push"
INPUT_UNKNOWN_OPCODE = "input.unknown_opcode"
INPUT_RESERVED = "input.reserved_opcode"
INPUT_PUBKEY = "input.crypto.pubkey"
INPUT_SIGENC = "input.crypto.sig_encoding"
INPUT_SCRIPT_LARGE = "input.script_too_large"
INPUT_DOMAIN_MISSING = "input.domain_missing"
STATE_UNKNOWN = "state.unknown_outpoint"
STATE_SPENT = "state.already_spent"
STATE_DOMAIN = "state.domain_conflict"
STATE_IMBALANCE = "state.imbalance"
RES_ELEMENT = "resource.element_too_large"
RES_STACK = "resource.stack_overflow"
RES_BUDGET = "resource.op_budget_exhausted"
RES_DEPTH = "resource.script_depth_exceeded"
COMP_UNDERFLOW = "compute.stack_underflow"
COMP_UNBALANCED = "compute.unbalanced_if"
COMP_VERIFY = "compute.verify"
COMP_RETURN = "compute.op_return"
COMP_EQUAL = "compute.equalverify"
COMP_SIG = "compute.crypto.sig"
COMP_THRESHOLD = "compute.crypto.threshold"
COMP_MULTI_POLICY = "compute.crypto.threshold_invalid"
COMP_FALSE = "compute.script_false"
COMP_EMPTY = "compute.script_empty"
COMP_DIRTY = "compute.dirty_stack"
ACCEPT = "accepted"

OP_0, OP_1 = 0x00, 0x51
OP_VERIFY, OP_RETURN = 0x69, 0x6A
OP_DUP, OP_DROP = 0x76, 0x75
OP_EQUALVERIFY = 0x88
OP_SHA256 = 0xA8
OP_RIPEMD160 = 0xA6
OP_HASH160, OP_HASH256 = 0xA9, 0xAA
OP_CHECKSIG, OP_CHECKMULTISIG = 0xAC, 0xAE


# ================= 独立密码学/编码（不 import rsv） ========================

def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def hash256(b: bytes) -> bytes:
    return sha256(sha256(b))


def hash160(b: bytes) -> bytes:
    h = hashlib.new("ripemd160")
    h.update(sha256(b))
    return h.digest()


def ripemd160(b: bytes) -> bytes:
    h = hashlib.new("ripemd160")
    h.update(b)
    return h.digest()


def op_push(data: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return b"\x00"
    if n <= 0x4B:
        return bytes([n]) + data
    if n <= 0xFF:
        return b"\x4c" + bytes([n]) + data
    if n <= 0xFFFF:
        return b"\x4d" + n.to_bytes(2, "little") + data
    return b"\x4e" + n.to_bytes(4, "little") + data


def der_privkey(seed: int) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(seed % (1 << 256), ec.SECP256K1())


def pub_compressed(priv: ec.EllipticCurvePrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint
    )


def sign(priv: ec.EllipticCurvePrivateKey, msg32: bytes) -> bytes:
    return priv.sign(msg32, ec.ECDSA(Prehashed(hashes.SHA256())))


def verify_lib(sig: bytes, msg32: bytes, pub: bytes) -> bool:
    """直接调成熟库的独立验签真值（测试交叉验证用的同一来源）。"""
    try:
        alg = bytes.fromhex("301006072a8648ce3d020106052b8104000a")
        bs = b"\x03" + _der_len(len(pub) + 1) + b"\x00" + pub
        spki = b"\x30" + _der_len(len(alg) + len(bs)) + alg + bs
        key = serialization.load_der_public_key(spki)
        key.verify(sig, msg32, ec.ECDSA(Prehashed(hashes.SHA256())))
        return True
    except Exception:
        return False


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n <= 0xFF:
        return bytes([0x81, n])
    if n <= 0xFFFF:
        return bytes([0x82, n >> 8, n & 0xFF])
    return bytes([0x83, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])


# ================= 独立规范化序列化 ======================================

def leb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def canonical_tx(tx: dict) -> bytes:
    buf = bytearray()
    buf += int(tx["version"]).to_bytes(4, "little")
    dom = tx["domain"].encode()
    buf += leb(len(dom)) + dom
    buf += int(tx.get("locktime", 0)).to_bytes(4, "little")
    buf += leb(len(tx["inputs"]))
    for inp in tx["inputs"]:
        txid = bytes.fromhex(inp["outpoint"]["txid"])
        assert len(txid) == 32
        buf += txid
        buf += int(inp["outpoint"]["index"]).to_bytes(4, "little")
        buf += int(inp["value"]).to_bytes(8, "little")  # witness 不参与
    buf += leb(len(tx["outputs"]))
    for out in tx["outputs"]:
        buf += int(out["value"]).to_bytes(8, "little")
        ps = bytes.fromhex(out["pubkey_script"])
        buf += leb(len(ps)) + ps
    return bytes(buf)


def txid_of(tx: dict) -> str:
    return hash256(canonical_tx(tx)).hex()


def message32(network: str, domain: str, tx: dict) -> bytes:
    cx = canonical_tx(tx)
    net, dom = network.encode(), domain.encode()
    body = (
        b"rsv-sighash-v1"
        + leb(len(net)) + net
        + leb(len(dom)) + dom
        + leb(len(cx)) + cx
    )
    return hash256(body)


def fixed_message(network: str, domain: str, canonical_hex: str) -> bytes:
    """脚本级向量用的摘要：canonical 字节直接给定。"""
    cx = bytes.fromhex(canonical_hex)
    net, dom = network.encode(), domain.encode()
    body = (
        b"rsv-sighash-v1"
        + leb(len(net)) + net
        + leb(len(dom)) + dom
        + leb(len(cx)) + cx
    )
    return hash256(body)


# ================= 脚本模板 ================= ============================

def p2pk_lock(pub: bytes) -> bytes:
    return op_push(pub) + bytes([OP_CHECKSIG])


def p2pkh_lock(pub: bytes) -> bytes:
    return bytes([OP_DUP, 0xA9]) + op_push(hash160(pub)) + bytes([OP_EQUALVERIFY, OP_CHECKSIG])


def p2ms_lock(m: int, pubs: list[bytes]) -> bytes:
    return (
        op_push(bytes([m]))
        + b"".join(op_push(p) for p in pubs)
        + op_push(bytes([len(pubs)]))
        + bytes([OP_CHECKMULTISIG])
    )


def w_p2pk(sig: bytes) -> bytes:
    return op_push(sig)


def w_p2pkh(sig: bytes, pub: bytes) -> bytes:
    return op_push(sig) + op_push(pub)


def w_p2ms(sigs: list[bytes]) -> bytes:
    return b"".join(op_push(s) for s in sigs)


# ================= 独立 bundle 评估（模板级第二实现） ======================

def parse_pushes(script: bytes) -> list[bytes]:
    """仅解析我们自己生成的模板脚本里的推送项（够独立评估 P2PK/P2PKH/P2MS）。"""
    out, i = [], 0
    while i < len(script):
        b = script[i]
        i += 1
        if b == 0x00:
            out.append(b"")
        elif 0x01 <= b <= 0x4B:
            out.append(script[i : i + b])
            i += b
        elif b in (0x4C, 0x4D, 0x4E):
            w = {0x4C: 1, 0x4D: 2, 0x4E: 4}[b]
            n = int.from_bytes(script[i : i + w], "little")
            i += w
            out.append(script[i : i + n])
            i += n
        elif 0x4F <= b <= 0x60:
            # OP_1NEGATE / OP_1..OP_16：压入脚本小整数
            out.append(b"\x81" if b == 0x4F else bytes([b - 0x50]))
        # 其余为非推送操作码，跳过（模板里推送位置只需要数据项）
    return out


def independent_template_check(witness: bytes, lock_hex: str, msg: bytes) -> str | None:
    """对已知模板做独立验签，返回 None=接受，否则返回预期失败码。"""
    lock = bytes.fromhex(lock_hex)
    w_items = parse_pushes(witness)
    # P2MS 模板以小整数 m 开头（推送 1 字节），包含 CHECKMULTISIG
    if lock[-1] == OP_CHECKMULTISIG:
        l_items = parse_pushes(lock[:-1])
        m = l_items[0][0]
        pubs = l_items[1:-1]
        n = l_items[-1][0]
        sigs = w_items
        if not (1 <= m <= n <= 16):
            return COMP_MULTI_POLICY
        if len(pubs) != n:
            return COMP_MULTI_POLICY
        if len(sigs) != m:
            return COMP_THRESHOLD  # 提供签名数就不足（正常构造不会发生）
        if len(set(pubs)) != n:
            return COMP_MULTI_POLICY
        if len(set(sigs)) != m:
            return COMP_MULTI_POLICY
        used: set[bytes] = set()
        matched = 0
        for s in sigs:
            for p in pubs:
                if p in used:
                    continue
                if verify_lib(s, msg, p):
                    used.add(p)
                    matched += 1
                    break
        return ACCEPT if matched >= m else COMP_THRESHOLD
    # P2PKH: DUP HASH160 <h> EQUALVERIFY CHECKSIG；witness = sig pub
    if lock[-1] == OP_CHECKSIG and OP_EQUALVERIFY in lock:
        sig, pub = w_items[0], w_items[1]
        h = parse_pushes(lock)[0]
        if hash160(pub) != h:
            return COMP_EQUAL
        return ACCEPT if verify_lib(sig, msg, pub) else COMP_SIG
    # P2PK
    if lock[-1] == OP_CHECKSIG:
        pub = parse_pushes(lock)[0]
        sig = w_items[0]
        return ACCEPT if verify_lib(sig, msg, pub) else COMP_SIG
    return ACCEPT  # 非模板脚本不由该函数评估


def independent_replay(genesis: dict, transactions: list[dict], network: str) -> list[dict]:
    """完全独立的状态机：跟踪 UTXO 集合/双花/域/金额/模板脚本。"""
    utxos: dict[tuple[str, int], dict] = {}
    spent: set[tuple[str, int]] = set()
    for c in genesis["utxos"]:
        utxos[(c["txid"], c["index"])] = c
    results = []
    for seq, tx in enumerate(transactions, start=1):
        code = ACCEPT
        try:
            if not tx.get("domain"):
                code = INPUT_DOMAIN_MISSING
                raise _Stop
            in_sum = 0
            msgs = {}
            seen = set()
            for inp in tx["inputs"]:
                k = (inp["outpoint"]["txid"], inp["outpoint"]["index"])
                if k in seen:
                    code = STATE_SPENT
                    raise _Stop
                seen.add(k)
                if k in spent:
                    code = STATE_SPENT
                    raise _Stop
                if k not in utxos:
                    code = STATE_UNKNOWN
                    raise _Stop
                coin = utxos[k]
                if coin["domain"] != tx["domain"]:
                    code = STATE_DOMAIN
                    raise _Stop
                in_sum += coin["value"]
                msgs[k] = message32(network, coin["domain"], tx)
            if in_sum != sum(o["value"] for o in tx["outputs"]):
                code = STATE_IMBALANCE
                raise _Stop
            for inp in tx["inputs"]:
                k = (inp["outpoint"]["txid"], inp["outpoint"]["index"])
                r = independent_template_check(
                    bytes.fromhex(inp["witness_script"]),
                    utxos[k]["pubkey_script"],
                    msgs[k],
                )
                if r != ACCEPT:
                    code = r
                    raise _Stop
            # 接受：更新独立状态
            for inp in tx["inputs"]:
                k = (inp["outpoint"]["txid"], inp["outpoint"]["index"])
                del utxos[k]
                spent.add(k)
            for j, o in enumerate(tx["outputs"]):
                utxos[(txid_of(tx), j)] = {
                    "txid": txid_of(tx), "index": j, "value": o["value"],
                    "pubkey_script": o["pubkey_script"], "domain": tx["domain"],
                }
        except _Stop:
            pass
        results.append({"seq": seq, "accepted": code == ACCEPT, "code": code})
    return results


class _Stop(Exception):
    pass


# ================= 构造夹具 ================= ============================

def genesis_outpoint(name: str) -> tuple[str, int]:
    return hash256(b"rsv-genesis/" + name.encode()).hex(), 0


def spend(coin_name: str, value: int, witness: bytes, out_pub: bytes,
          domain: str = DOMAIN_A, out_value: int | None = None) -> dict:
    txid0, idx0 = genesis_outpoint(coin_name)
    return {
        "version": 1,
        "domain": domain,
        "inputs": [{
            "outpoint": {"txid": txid0, "index": idx0},
            "value": value,
            "witness_script": witness.hex(),
        }],
        "outputs": [{
            "value": value if out_value is None else out_value,
            "pubkey_script": p2pk_lock(out_pub).hex(),
        }],
        "locktime": 0,
    }


def build_all() -> dict:
    seeds = {"a": 11, "b": 22, "c": 33, "d": 44, "e": 55}
    keys = {k: der_privkey(v) for k, v in seeds.items()}
    pubs = {k: pub_compressed(v) for k, v in keys.items()}

    # ---- 创世纪 ----
    specs = [
        ("coin_p2pk_a", p2pk_lock(pubs["a"]), 1000, DOMAIN_A),
        ("coin_p2pkh_b", p2pkh_lock(pubs["b"]), 2000, DOMAIN_A),
        ("coin_2of3", p2ms_lock(2, [pubs["a"], pubs["b"], pubs["c"]]), 3000, DOMAIN_A),
        ("coin_domain_b", p2pk_lock(pubs["d"]), 500, DOMAIN_B),
    ]
    coins = []
    for name, script, value, dom in specs:
        t0, i0 = genesis_outpoint(name)
        coins.append({"txid": t0, "index": i0, "value": value,
                      "pubkey_script": script.hex(), "domain": dom})
    gid = hashlib.sha256(
        json.dumps({"utxos": coins}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    genesis = {"genesis_id": gid, "network": NETWORK, "utxos": coins}

    # ---- 各类签名好/坏交易（witness 不进摘要，先建 tx 再签） ----
    def signed(coin, value, wfunc, out="a", domain=DOMAIN_A, **kw):
        tx = spend(coin, value, b"", pubs[out], domain=domain, **kw)
        m = message32(NETWORK, domain, tx)
        tx["inputs"][0]["witness_script"] = wfunc(m).hex()
        return tx

    good_p2pk = signed("coin_p2pk_a", 1000, lambda m: w_p2pk(sign(keys["a"], m)))
    good_p2pkh = signed("coin_p2pkh_b", 2000,
                        lambda m: w_p2pkh(sign(keys["b"], m), pubs["b"]))
    ms_ca = signed("coin_2of3", 3000,
                   lambda m: w_p2ms([sign(keys["c"], m), sign(keys["a"], m)]))
    # 注意：ECDSA 对同一 (key,msg) 的两次独立签名可能不同（随机/反随机性），
    # “重复签名”向量必须复用同一个签名字节串，否则测不到重复计数分支。
    def dup_witness(m):
        sa = sign(keys["a"], m)
        return w_p2ms([sa, sa])

    dup_sig = signed("coin_2of3", 3000, dup_witness)
    threshold_short = signed(
        "coin_2of3", 3000,
        lambda m: w_p2ms([sign(keys["a"], m), sign(keys["d"], m)]),
    )

    # 错误域签名：交易域=B（过状态检查），但签名是在 domain A 的摘要上
    wsd = spend("coin_domain_b", 500, b"", pubs["d"], domain=DOMAIN_B)
    sig_wsd = sign(keys["d"], message32(NETWORK, DOMAIN_A, wsd))
    wsd["inputs"][0]["witness_script"] = w_p2pk(sig_wsd).hex()

    # 错误交易域：交易域=A 去花 domain B 的币（状态层拦截）
    wdom = spend("coin_domain_b", 500, b"", pubs["d"], domain=DOMAIN_A)
    sig_wdom = sign(keys["d"], message32(NETWORK, DOMAIN_A, wdom))
    wdom["inputs"][0]["witness_script"] = w_p2pk(sig_wdom).hex()

    imb = signed("coin_p2pk_a", 1000, lambda m: w_p2pk(sign(keys["a"], m)),
                 out_value=999)
    unk = spend("coin_p2pk_a", 1000, b"", pubs["a"])
    unk["inputs"][0]["outpoint"]["txid"] = "ff" * 32
    # outpoint 变了 -> 摘要变了，需要用新摘要重签（否则会先撞 sig 失败）
    unk["inputs"][0]["witness_script"] = w_p2pk(
        sign(keys["a"], message32(NETWORK, DOMAIN_A, unk))
    ).hex()

    # 第二笔合法 P2PK（同一枚币）用于双花 bundle
    dbl = signed("coin_p2pk_a", 1000, lambda m: w_p2pk(sign(keys["a"], m)))

    bundles = {
        "happy_path.json": {
            "bundle_id": "happy-path",
            "description": "P2PK、P2PKH、2-of-3（c/a 乱序签名）全部接受",
            "genesis": genesis,
            "transactions": [good_p2pk, good_p2pkh, ms_ca],
        },
        "failure_catalog.json": {
            "bundle_id": "failure-catalog",
            "description": "重复签名、阈值不足、错误交易域、错误域签名、金额不守恒、未知 outpoint",
            "genesis": genesis,
            "transactions": [dup_sig, threshold_short, wdom, wsd, imb, unk],
        },
        "double_spend.json": {
            "bundle_id": "double-spend-order",
            "description": "同一 outpoint 连交两次：先接受，后 state.already_spent",
            "genesis": genesis,
            "transactions": [good_p2pk, dbl],
        },
    }
    for b in bundles.values():
        b["expected"] = independent_replay(genesis, b["transactions"], NETWORK)

    # ---- 脚本级独立向量（直接跑栈机，不涉链状态） ----
    script_vectors = build_script_vectors(keys, pubs)

    keys_doc = {
        "note": "合成测试密钥，由固定小整数标量派生，严禁用于任何真实场景",
        "seeds": seeds,
        "pubkeys_compressed_hex": {k: v.hex() for k, v in pubs.items()},
    }
    return {
        "keys": keys_doc,
        "genesis": genesis,
        "bundles": bundles,
        "script_vectors": script_vectors,
    }


def vec(vid, desc, unlock, lock, msg, expected, cross=None):
    return {
        "id": vid,
        "description": desc,
        "network": NETWORK,
        "domain": DOMAIN_A,
        "canonical_tx": "00" * 8,  # 8 字节合成“交易”，摘要依然唯一绑定域
        "message32": msg.hex(),
        "unlock_script": unlock.hex(),
        "lock_script": lock.hex(),
        "expected": expected,
        "cross_check": cross,
    }


def acc():
    return {"accepted": True, "category": None, "code": ACCEPT}


def rej(category, code):
    return {"accepted": False, "category": category, "code": code}


def build_script_vectors(keys, pubs) -> list[dict]:
    can = "00" * 8
    mA = fixed_message(NETWORK, DOMAIN_A, can)
    mB = fixed_message(NETWORK, DOMAIN_B, can)

    def s(k, m):
        return sign(keys[k], m)

    V = []

    # --- 验签基础与交叉验证 ---
    sigA = s("a", mA)
    V.append(vec("p2pk_ok", "P2PK 正确签名", w_p2pk(sigA), p2pk_lock(pubs["a"]), mA,
                 acc(), {"lib_verify": verify_lib(sigA, mA, pubs["a"]),
                         "lib_verify_wrong_msg": verify_lib(sigA, mB, pubs["a"])}))
    sigB_on_A = s("b", mA)
    V.append(vec("p2pk_wrong_key", "用 b 的签名配 a 的公钥 -> compute.crypto.sig",
                 w_p2pk(sigB_on_A), p2pk_lock(pubs["a"]), mA,
                 rej("compute", COMP_SIG),
                 {"lib_verify": verify_lib(sigB_on_A, mA, pubs["a"])}))
    V.append(vec("p2pk_wrong_domain_msg", "签名针对 domain B，栈机在 domain A 摘要上验 -> sig",
                 w_p2pk(s("a", mB)), p2pk_lock(pubs["a"]), mA,
                 rej("compute", COMP_SIG),
                 {"lib_verify_correct_domain": verify_lib(s("a", mB), mB, pubs["a"]),
                  "lib_verify_wrong_domain": verify_lib(s("a", mB), mA, pubs["a"])}))
    V.append(vec("p2pkh_ok", "P2PKH 正确签名+公钥",
                 w_p2pkh(s("b", mA), pubs["b"]), p2pkh_lock(pubs["b"]), mA, acc(),
                 {"hash160_pub": hash160(pubs["b"]).hex()}))
    V.append(vec("p2pkh_other_pub", "P2PKH 携带不匹配的公钥 -> equalverify",
                 w_p2pkh(s("a", mA), pubs["a"]), p2pkh_lock(pubs["b"]), mA,
                 rej("compute", COMP_EQUAL)))

    # --- m-of-n 门槛、重复计数、顺序 ---
    l23 = p2ms_lock(2, [pubs["a"], pubs["b"], pubs["c"]])
    V.append(vec("ms_2of3_order_ca", "2-of-3：提供 c,a（逆序），顺序无关 -> 接受",
                 w_p2ms([s("c", mA), s("a", mA)]), l23, mA, acc()))
    V.append(vec("ms_2of3_order_ac", "2-of-3：提供 a,c（顺序互换）-> 接受且与逆序结果一致",
                 w_p2ms([s("a", mA), s("c", mA)]), l23, mA, acc()))
    V.append(vec("ms_2of3_ab", "2-of-3：a,b -> 接受",
                 w_p2ms([s("a", mA), s("b", mA)]), l23, mA, acc()))
    V.append(vec("ms_1of3_one_valid", "门槛边界：1-of-3 只给 a -> 接受",
                 w_p2ms([s("a", mA)]), p2ms_lock(1, [pubs["a"], pubs["b"], pubs["c"]]),
                 mA, acc()))
    V.append(vec("ms_duplicate_signature", "同一签名推两次凑 m=2 -> 禁止重复计数",
                 w_p2ms([sigA, sigA]), l23, mA, rej("compute", COMP_MULTI_POLICY)))
    V.append(vec("ms_duplicate_pubkey_policy", "策略公钥 [a,a,b] 重复 -> 拒绝",
                 w_p2ms([s("a", mA), s("b", mA)]),
                 p2ms_lock(2, [pubs["a"], pubs["a"], pubs["b"]]), mA,
                 rej("compute", COMP_MULTI_POLICY)))
    V.append(vec("ms_threshold_not_met", "a 有效 + d 不在策略公钥中 -> 有效数不足 2",
                 w_p2ms([s("a", mA), s("d", mA)]), l23, mA,
                 rej("compute", COMP_THRESHOLD),
                 {"lib_verify_d_against_a": verify_lib(s("d", mA), mA, pubs["a"])}))
    # m=0：witness 无签名，锁里 m 字节为 0
    V.append(vec("ms_m_zero", "m=0 非法策略 -> threshold_invalid",
                 b"", p2ms_lock(0, [pubs["a"], pubs["b"]]), mA,
                 rej("compute", COMP_MULTI_POLICY)))
    # n<m：锁 m=3 n=2；witness 提供 3 个签名
    V.append(vec("ms_m_gt_n", "m=3,n=2 非法策略 -> threshold_invalid",
                 w_p2ms([s("a", mA), s("b", mA), s("c", mA)]),
                 p2ms_lock(3, [pubs["a"], pubs["b"]]), mA,
                 rej("compute", COMP_MULTI_POLICY)))

    # --- 编码错误 ---
    V.append(vec("bad_push_truncated", "0x02 直接推送但只剩 1 字节 -> malformed_push",
                 b"", b"\x02\xab", mA, rej("input", INPUT_MALFORMED_PUSH)))
    V.append(vec("bad_push_nonminimal", "5 字节数据用 PUSHDATA1 -> 非最小编码",
                 b"", b"\x4c\x05" + b"abcde", mA, rej("input", INPUT_MALFORMED_PUSH)))
    V.append(vec("unknown_opcode", "白名单外操作码 0xEF -> unknown_opcode",
                 b"", b"\xef", mA, rej("input", INPUT_UNKNOWN_OPCODE)))
    V.append(vec("reserved_opcode", "经典保留字节 0x50 -> reserved_opcode",
                 b"", b"\x50", mA, rej("input", INPUT_RESERVED)))
    V.append(vec("unbalanced_if", "IF 没有 ENDIF -> unbalanced_if",
                 b"", bytes([OP_1, 0x63, OP_1]), mA, rej("compute", COMP_UNBALANCED)))
    V.append(vec("oversized_script", "脚本超过 2048 字节 -> script_too_large",
                 b"", b"\x61" * 2049, mA, rej("input", INPUT_SCRIPT_LARGE)))
    V.append(vec("oversized_element", "witness 推送 521 字节元素 -> element_too_large",
                 op_push(b"\xaa" * 521), b"", mA, rej("resource", RES_ELEMENT)))
    V.append(vec("stack_overflow", "连续压 65 个元素 -> stack_overflow(64)",
                 b"".join(bytes([1, 0x01]) for _ in range(65)), b"", mA,
                 rej("resource", RES_STACK)))
    V.append(vec("nested_if_too_deep", "9 层嵌套 IF -> script_depth_exceeded(8)",
                 b"",
                 (b"".join(bytes([OP_1, 0x63]) for _ in range(9))
                  + bytes([OP_1]) + b"\x68" * 9),
                 mA, rej("resource", RES_DEPTH)))

    # --- 栈下溢 / 预算 ---
    V.append(vec("stack_underflow_drop", "空栈 OP_DROP -> stack_underflow",
                 b"", bytes([OP_DROP]), mA, rej("compute", COMP_UNDERFLOW)))
    V.append(vec("stack_underflow_swap", "单元素 OP_SWAP -> stack_underflow",
                 bytes([OP_1, 0x7C]), b"", mA, rej("compute", COMP_UNDERFLOW)))
    V.append(vec("budget_exhausted", "129 条 NOP 超过 128 步预算 -> op_budget_exhausted",
                 b"", b"\x61" * 129, mA, rej("resource", RES_BUDGET),
                 {"instruction_count": 129, "budget": 128}))

    # --- 控制流 / 终态 ---
    V.append(vec("op_return_hit", "OP_RETURN -> compute.op_return",
                 b"", bytes([OP_RETURN]), mA, rej("compute", COMP_RETURN)))
    V.append(vec("verify_false", "OP_0 OP_VERIFY -> compute.verify",
                 b"", bytes([OP_0, OP_VERIFY]), mA, rej("compute", COMP_VERIFY)))
    V.append(vec("equalverify_fail", "1 与 2 OP_EQUALVERIFY -> equalverify",
                 b"", bytes([OP_1, 0x52, OP_EQUALVERIFY]), mA,
                 rej("compute", COMP_EQUAL)))
    V.append(vec("final_false", "只剩 OP_0 -> script_false",
                 b"", bytes([OP_0]), mA, rej("compute", COMP_FALSE)))
    V.append(vec("final_empty", "空解锁+空锁定 -> script_empty",
                 b"", b"", mA, rej("compute", COMP_EMPTY)))
    V.append(vec("dirty_stack", "留下两个真元素 -> dirty_stack",
                 b"", bytes([OP_1, OP_1]), mA, rej("compute", COMP_DIRTY)))
    V.append(vec("if_true_branch", "OP_1 IF 1 ELSE 0 ENDIF -> 接受",
                 b"", bytes([OP_1, 0x63, OP_1, 0x67, OP_0, 0x68]), mA, acc()))
    V.append(vec("if_false_branch", "OP_0 IF 0 ELSE 1 ENDIF -> 接受（走 ELSE）",
                 b"", bytes([OP_0, 0x63, OP_0, 0x67, OP_1, 0x68]), mA, acc()))

    # 推送顺序约定：先预期摘要、后原像。锁脚本 OP_SHA256 弹出原像压回摘要，
    # 栈变为 [expected, actual]，OP_EQUALVERIFY 弹出比较，最后 OP_1 留一个真值。
    pre = b"rsv-hash-preimage"
    V.append(vec("hash_sha256", "OP_SHA256 与 hashlib 对拍",
                 op_push(sha256(pre)) + op_push(pre),
                 bytes([OP_SHA256, OP_EQUALVERIFY, OP_1]),
                 mA, acc(), {"expected_sha256": sha256(pre).hex()}))
    V.append(vec("hash_sha256_negative", "错误预期摘要 -> equalverify",
                 op_push(b"\x00" * 32) + op_push(pre),
                 bytes([OP_SHA256, OP_EQUALVERIFY, OP_1]),
                 mA, rej("compute", COMP_EQUAL),
                 {"expected_sha256": sha256(pre).hex()}))
    V.append(vec("hash_hash256", "OP_HASH256 = sha256(sha256(x))",
                 op_push(hash256(pre)) + op_push(pre),
                 bytes([OP_HASH256, OP_EQUALVERIFY, OP_1]),
                 mA, acc(), {"expected_hash256": hash256(pre).hex()}))
    V.append(vec("hash_hash160", "OP_HASH160 = ripemd160(sha256(x))",
                 op_push(hash160(pre)) + op_push(pre),
                 bytes([OP_HASH160, OP_EQUALVERIFY, OP_1]),
                 mA, acc(), {"expected_hash160": hash160(pre).hex()}))
    V.append(vec("hash_ripemd160", "OP_RIPEMD160",
                 op_push(ripemd160(pre)) + op_push(pre),
                 bytes([OP_RIPEMD160, OP_EQUALVERIFY, OP_1]),
                 mA, acc(), {"expected_ripemd160": ripemd160(pre).hex()}))

    # --- 哈希锁 + 验签组合（原子性体现：preimage 与签名都要对） ---
    # 栈脚本执行顺序：witness 先执行（sig, secret 入栈），lock 再执行
    secret = b"correct-horse-battery-staple"
    hl_lock = bytes([OP_SHA256]) + op_push(sha256(secret)) + bytes(
        [OP_EQUALVERIFY]) + op_push(pubs["a"]) + bytes([OP_CHECKSIG])
    hl_wit_good = op_push(s("a", mA)) + op_push(secret)
    # 执行：SHA256 弹 secret 压 h；EQUALVERIFY 与锁中预期比较；CHECKSIG 弹 sig/pub
    V.append(vec("hashlock_ok", "哈希锁：正确原像+正确签名 -> 接受",
                 hl_wit_good, hl_lock, mA, acc(),
                 {"expected_sha256": sha256(secret).hex()}))
    hl_wit_bad = op_push(s("a", mA)) + op_push(b"wrong-secret")
    V.append(vec("hashlock_bad_preimage", "哈希锁原像错误 -> equalverify，不进入验签",
                 hl_wit_bad, hl_lock, mA, rej("compute", COMP_EQUAL)))

    # --- 公钥/签名结构错误（input 类优先） ---
    V.append(vec("bad_pubkey_bytes", "33 字节非法公钥 -> input.crypto.pubkey",
                 op_push(sigA) + op_push(b"\x02" + b"\x00" * 32),
                 bytes([OP_CHECKSIG]), mA, rej("input", INPUT_PUBKEY)))
    V.append(vec("bad_signature_der", "签名不是合法 DER -> input.crypto.sig_encoding",
                 op_push(b"\x30\x00") + op_push(pubs["a"]),
                 bytes([OP_CHECKSIG]), mA, rej("input", INPUT_SIGENC)))

    return V


def main(root: Path) -> None:
    fx = root / "fixtures"
    (fx / "bundles").mkdir(parents=True, exist_ok=True)
    data = build_all()
    (fx / "keys.json").write_text(json.dumps(data["keys"], indent=2, ensure_ascii=False), "utf-8")
    (fx / "genesis.json").write_text(
        json.dumps(data["genesis"], indent=2, ensure_ascii=False), "utf-8"
    )
    for name, bundle in data["bundles"].items():
        (fx / "bundles" / name).write_text(
            json.dumps(bundle, indent=2, ensure_ascii=False), "utf-8"
        )
    (fx / "script_vectors.json").write_text(
        json.dumps({"network": NETWORK, "vectors": data["script_vectors"]},
                   indent=2, ensure_ascii=False),
        "utf-8",
    )
    n_vec = len(data["script_vectors"])
    print(f"genesis coins={len(data['genesis']['utxos'])} "
          f"bundles={len(data['bundles'])} script_vectors={n_vec}")


if __name__ == "__main__":
    main(Path(__file__).resolve().parents[1])
