"""独立工具：生成确定性合成夹具。

关键独立性保证（满足“参考答案不能全部由被测核心实现自身生成”）：
- 密钥与签名使用 **`ecdsa` 库**（纯 Python 实现）生成与签署，被测核心使用
  **`cryptography`/OpenSSL** 验签，二者实现来源不同；
- 待签摘要由本工具自行规范化 JSON + hashlib 计算（fixture_signing_digest），
  并与 stackvm.sighash 做一次**一致性断言**（防夹具本身写错），但预期通过/失败
  分类完全由本文件中手工枚举的用例清单决定，VM 不参与“生成答案”。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import ecdsa
from ecdsa import SECP256k1, SigningKey

from stackvm.config import load_settings
from stackvm.sighash import sighash_document
from stackvm.transaction import Transaction, TxInput, TxOutput, canonical_json, txid_of
from stackvm import opcodes as O
from stackvm.script import assemble

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

# 固定种子：夹具必须可确定性重建（这些是公开的测试密钥，绝无真实资金）
KEY_SEEDS = {
    "alice": b"stackvm-fixture-seed::alice::v1",
    "bob": b"stackvm-fixture-seed::bob::v1",
    "carol": b"stackvm-fixture-seed::carol::v1",
    "dave": b"stackvm-fixture-seed::dave::v1",
}
WRONG_DOMAIN = "STACKVM.SIGHASH/WRONG-DOMAIN"
SECRET = b"correct-horse-battery-staple"
OUT_VALUE = 1000
RECEIVE_PUB_NAME = "alice"  # 所有正常花费的收款输出都回到 alice


def _low_s_der(der_sig: bytes) -> bytes:
    """把 DER 签名规范化为 low-S（s > order/2 时取 s'=order-s）。

    与 Bitcoin 脚本的低 S 规则一致；避免签名延展性。被测核心（cryptography）
    侧另有 high-S 拒绝，二者形成交叉约束。
    """
    r, s = ecdsa.util.sigdecode_der(der_sig, SECP256k1.order)
    if s > SECP256k1.order // 2:
        s = SECP256k1.order - s
    return ecdsa.util.sigencode_der(r, s, SECP256k1.order)


def rfc6979_signature(sk: SigningKey, digest: bytes) -> bytes:
    """RFC6979 确定性签名（DER，规范化为 low-S）。"""
    raw = sk.sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=ecdsa.util.sigencode_der)
    return _low_s_der(raw)


def derive_key(name: str) -> SigningKey:
    seed = KEY_SEEDS[name]
    return SigningKey.from_string(hashlib.sha256(seed).digest(), curve=SECP256k1)


def pub_compressed(sk: SigningKey) -> bytes:
    pt = sk.get_verifying_key().pubkey.point
    prefix = b"\x02" if pt.y() % 2 == 0 else b"\x03"
    return prefix + pt.x().to_bytes(32, "big")


def fixture_digest(tx: Transaction, prev_scripts: list[bytes], domain_tag: str) -> bytes:
    """工具侧独立计算的签名摘要（不调用 stackvm.sighash.signature_digest）。"""
    doc = sighash_document(tx, prev_scripts, domain_tag)  # 仅复用文档形状
    return hashlib.sha256(canonical_json(doc).encode()).digest()


def p2pkh_lock(pub: bytes) -> bytes:
    from stackvm import hashes as H
    return assemble([O.Op.OP_DUP, O.Op.OP_HASH160, H.hash160(pub),
                     O.Op.OP_EQUALVERIFY, O.Op.OP_CHECKSIG])


def multisig_lock(pubs: list[bytes], m: int) -> bytes:
    return assemble([m] + pubs + [len(pubs), O.Op.OP_CHECKMULTISIG])


def p2pk_lock(pub: bytes) -> bytes:
    return assemble([pub, O.Op.OP_CHECKSIG])


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    domain = settings.sighash.domain_tag

    keys = {name: derive_key(name) for name in KEY_SEEDS}
    pubs = {name: pub_compressed(sk) for name, sk in keys.items()}

    # ---- 密钥清单（公开测试密钥） ----
    key_doc = {
        "note": "确定性生成的本地测试密钥（RFC6979 种子公开），禁止用于任何真实场景",
        "curve": "secp256k1",
        "encoding": {"private_wif_style": "32-byte scalar hex (TEST ONLY)",
                     "public": "SEC1 compressed 33-byte hex"},
        "keys": {
            name: {
                "priv_hex": keys[name].to_string().hex(),
                "pub_hex": pubs[name].hex(),
                "seed_hex": KEY_SEEDS[name].hex(),
            } for name in KEY_SEEDS
        },
    }
    (FIXTURES / "keys.json").write_text(json.dumps(key_doc, indent=2, ensure_ascii=False), "utf-8")

    # ---- 21 个 genesis 输出，每个 1000，锁脚本各不相同（用例 00..20） ----
    from stackvm import hashes as H

    locks: list[bytes] = []
    labels: list[str] = []

    # 00: P2PK alice 正常
    locks.append(p2pk_lock(pubs["alice"])); labels.append("p2pk_ok")
    # 01: P2PKH bob 正常
    locks.append(p2pkh_lock(pubs["bob"])); labels.append("p2pkh_ok")
    # 02: 2-of-3 (alice,bob,carol) 正常，给 alice+bob 两个签
    locks.append(multisig_lock([pubs["alice"], pubs["bob"], pubs["carol"]], 2)); labels.append("multisig_2of3_ab_ok")
    # 03: 2-of-3 但只用一个签 → 门槛不足
    locks.append(multisig_lock([pubs["alice"], pubs["bob"], pubs["carol"]], 2)); labels.append("multisig_2of3_only_one")
    # 04: 1-of-2 alice 单签 → 边界成功
    locks.append(multisig_lock([pubs["alice"], pubs["bob"]], 1)); labels.append("multisig_1of2_boundary_ok")
    # 05: 2-of-2 两个签名（重复 alice 两次）→ 重复计数
    locks.append(multisig_lock([pubs["alice"], pubs["bob"]], 2)); labels.append("multisig_duplicate_pubkey")
    # 06: 2-of-2 提供 bob,alice 两个有效签但顺序颠倒 → 无法有序匹配
    locks.append(multisig_lock([pubs["alice"], pubs["bob"]], 2)); labels.append("multisig_order_swap")
    # 07: P2PK 错误域标签签名
    locks.append(p2pk_lock(pubs["alice"])); labels.append("p2pk_wrong_domain")
    # 08: 预算耗尽 —— 锁定脚本 200 个 NOP 后才 P2PK，解锁段 1 步 +
    #     锁段每 NOP 1 步，在到达验签前用尽 200 步预算
    locks.append(assemble([O.Op.OP_NOP] * 200 + [pubs["alice"], O.Op.OP_CHECKSIG]))
    labels.append("budget_exhausted")
    # 09: 锁定脚本里直接 DROP，栈下溢（解锁不压任何元素）
    locks.append(assemble([O.Op.OP_DROP, 1])); labels.append("stack_underflow")
    # 10: HASH160 原像锁，正确秘密
    locks.append(assemble([O.Op.OP_HASH160, H.hash160(SECRET), O.Op.OP_EQUALVERIFY, 1])); labels.append("hashlock_ok")
    # 11: HASH160 原像锁，错误秘密
    locks.append(assemble([O.Op.OP_HASH160, H.hash160(SECRET), O.Op.OP_EQUALVERIFY, 1])); labels.append("hashlock_wrong_secret")
    # 12: SHA256 原像锁，正确（核验 SHA256 操作）
    locks.append(assemble([O.Op.OP_SHA256, hashlib.sha256(SECRET).digest(), O.Op.OP_EQUALVERIFY, 1])); labels.append("sha256_preimage_ok")
    # 13: HASH256(txid 风格) 原像锁，正确
    locks.append(assemble([O.Op.OP_HASH256, H.hash256(SECRET), O.Op.OP_EQUALVERIFY, 1])); labels.append("hash256_preimage_ok")
    # 14: RIPEMD160 原像锁，正确（核验 RIPEMD160 操作）
    locks.append(assemble([O.Op.OP_RIPEMD160, H.ripemd160(SECRET), O.Op.OP_EQUALVERIFY, 1])); labels.append("ripemd160_preimage_ok")
    # 15: 元素过大（解锁尝试直接压入 256 字节，超出 255 上限）
    locks.append(p2pk_lock(pubs["alice"])); labels.append("element_too_large")
    # 16: 未知操作码锁（0x62 在白名单之外）
    locks.append(bytes([0x62])); labels.append("unknown_opcode")
    # 17: 干净栈违规：锁是 OP_1 OP_2（结束留两个元素）
    locks.append(assemble([1, 2])); labels.append("unclean_stack")
    # 18: IF 条件真分支成功
    locks.append(assemble([O.Op.OP_IF, 1, O.Op.OP_ELSE, O.Op.OP_RETURN, O.Op.OP_ENDIF])); labels.append("if_true_branch_ok")
    # 19: IF 条件假分支：解锁给 OP_0，走 ELSE 分支放 OP_1
    locks.append(assemble([O.Op.OP_IF, O.Op.OP_RETURN, O.Op.OP_ELSE, 1, O.Op.OP_ENDIF])); labels.append("if_false_branch_ok")
    # 20: P2PK alice 正常（保留给服务双花/重复提交演示，正常后再花一次）
    locks.append(p2pk_lock(pubs["alice"])); labels.append("service_demo_ok")

    assert len(locks) == 21
    genesis_tx = Transaction(
        version=1, locktime=0, inputs=(),
        outputs=tuple(TxOutput(value=OUT_VALUE, script=lk.hex()) for lk in locks),
    )
    gtxid = txid_of(genesis_tx)
    genesis_doc = {
        "note": "零输入铸币测试交易，仅由 Store.bootstrap_genesis 在空库接受",
        "total_minted": OUT_VALUE * len(locks),
        "tx": genesis_tx.wire(),
        "output_labels": labels,
    }
    (FIXTURES / "genesis.json").write_text(
        json.dumps(genesis_doc, indent=2, ensure_ascii=False), "utf-8")

    receive_lock = p2pk_lock(pubs[RECEIVE_PUB_NAME])
    out = [TxOutput(value=OUT_VALUE, script=receive_lock.hex())]

    def spend_tx(vout: int, unlock: bytes) -> Transaction:
        return Transaction(version=1, locktime=0,
                           inputs=(TxInput(txid=gtxid, vout=vout, unlock=unlock.hex()),),
                           outputs=tuple(out))

    # 预构造各用例交易（先给空 unlock 占位，拿到摘要后再签、再回填 unlock）
    cases = {}

    def finalize(case_id: str, label: str, expect: str, reason: str,
                 prev_lock: bytes, unlocker, *,
                 signing_domain: str = domain) -> None:
        """构造花费 vout=case_id 的交易并由独立 ecdsa 库签名。

        unlocker(digest) -> unlock bytes。摘要不含 unlock，因此单遍即可。
        """
        # 先放占位空 unlock 计算交易线体（unlock 不进摘要，进 txid 的是最终值，
        # 所以这里直接先拿 digest，再组装最终 tx 后重新算 txid；digest 不变）
        placeholder = spend_tx(int(case_id), b"")
        digest = fixture_digest(placeholder, [prev_lock], signing_domain)
        unlock = unlocker(digest)
        tx = Transaction(
            version=1, locktime=0,
            inputs=(TxInput(txid=gtxid, vout=int(case_id), unlock=unlock.hex()),),
            outputs=tuple(out))
        # 摘要与 unlock 无关：最终交易的摘要必须仍是同一个 digest
        assert fixture_digest(tx, [prev_lock], signing_domain) == digest
        cases[case_id] = {
            "label": label, "expected": expect, "reason": reason,
            "prev_vout": int(case_id),
            "tx": tx.wire(),
            "txid": txid_of(tx),
            "signing_domain": signing_domain,
            "digest_hex": digest.hex(),
            "prev_lock": prev_lock.hex(),
        }

    # 00 P2PK ok: unlock = sig
    finalize("00", labels[0], "OK",
             "alice 用正确域标签对完整交易摘要签名，P2PK 验签通过",
             locks[0],
             lambda d: assemble([rfc6979_signature(keys["alice"], d)]))

    # 01 P2PKH ok: unlock = sig + pub
    finalize("01", labels[1], "OK",
             "bob 的 P2PKH：DUP HASH160 比对 pub，CHECKSIG 通过",
             locks[1],
             lambda d: assemble([rfc6979_signature(keys["bob"], d), pubs["bob"]]))

    # 02 2-of-3 ok: unlock = sigA sigB
    finalize("02", labels[2], "OK",
             "alice、bob 顺序签名满足 2-of-3 门槛，两把不同公钥各计一次",
             locks[2],
             lambda d: assemble([rfc6979_signature(keys["alice"], d),
                                 rfc6979_signature(keys["bob"], d)]))

    # 03 2-of-3 only one sig → m 弹出后栈上只剩 1 个签名，第二签缺失即下溢类门槛失败
    finalize("03", labels[3], "STACK_UNDERFLOW",
             "m=2 但只提供 1 个签名，取第二个签名时栈下溢，门槛无法满足",
             locks[3],
             lambda d: assemble([rfc6979_signature(keys["alice"], d)]))

    # 04 1-of-2 ok
    finalize("04", labels[4], "OK",
             "1-of-2 边界：只需 alice 一把，门槛满足",
             locks[4],
             lambda d: assemble([rfc6979_signature(keys["alice"], d)]))

    # 05 duplicate: alice 同一签名出现两次（pub 列表 alice,bob，2-of-2）
    finalize("05", labels[5], "SIG_DUPLICATED",
             "同一签名/公钥被提交两次，重复计数必须被拒绝",
             locks[5],
             lambda d: assemble([rfc6979_signature(keys["alice"], d),
                                 rfc6979_signature(keys["alice"], d)]))

    # 06 order swap: 栈中 [sigB, sigA]，有序匹配时第二签无法回溯到 alice
    finalize("06", labels[6], "THRESHOLD_NOT_MET",
             "两个签名均有效但顺序颠倒，有序贪心匹配无法让第二签回溯到 alice",
             locks[6],
             lambda d: assemble([rfc6979_signature(keys["bob"], d),
                                 rfc6979_signature(keys["alice"], d)]))

    # 07 wrong domain
    finalize("07", labels[7], "SIG_INVALID",
             "签名使用错误交易域标签，摘要不同，CHECKSIG 失败",
             locks[7],
             lambda d: assemble([rfc6979_signature(keys["alice"], d)]),
             signing_domain=WRONG_DOMAIN)

    # 08 budget exhausted: 锁脚本 200 个 NOP 后才 P2PK；
    #    解锁段 1 步 + 锁段 199 个 NOP 后第 200 个 NOP 无预算
    finalize("08", labels[8], "BUDGET_EXHAUSTED",
             "200 个 NOP 在到达 CHECKSIG 前耗尽全部 200 步预算（验签永不发生）",
             locks[8],
             lambda d: assemble([rfc6979_signature(keys["alice"], d)]))

    # 09 underflow: unlock 空，锁第一指令 DROP
    tx09 = spend_tx(9, b"")
    cases["09"] = {
        "label": labels[9], "expected": "STACK_UNDERFLOW",
        "reason": "解锁不压入任何元素，锁定脚本首指令 DROP 从空栈弹出",
        "prev_vout": 9, "tx": tx09.wire(), "txid": txid_of(tx09),
        "signing_domain": domain,
        "digest_hex": fixture_digest(tx09, [locks[9]], domain).hex(),
        "prev_lock": locks[9].hex(),
    }

    # 10/12/13/14 哈希原像正确
    for idx in (10, 12, 13, 14):
        finalize(str(idx), labels[idx], "OK",
                 f"提供正确原像，{labels[idx]} 比对通过并留下真元素",
                 locks[idx],
                 lambda d, secret=SECRET: assemble([secret]))

    # 11 错误秘密
    finalize("11", labels[11], "EVAL_FALSE",
             "提供错误原像，HASH160 结果与锁中哈希不等，EQUALVERIFY 失败",
             locks[11],
             lambda d: assemble([SECRET + b"-wrong"]))

    # 15 element too large: 直接构造 256 字节原始压入：0x4c 0xff? 256 用 PUSHDATA2
    raw256 = bytes([O.Op.OP_PUSHDATA2, 0x00, 0x01]) + b"\xaa" * 256
    tx15 = Transaction(
        version=1, locktime=0,
        inputs=(TxInput(txid=gtxid, vout=15, unlock=raw256.hex()),),
        outputs=tuple(out))
    cases["15"] = {
        "label": labels[15], "expected": "ELEMENT_TOO_LARGE",
        "reason": "解锁压入 256 字节元素，超过 255 字节上限",
        "prev_vout": 15, "tx": tx15.wire(), "txid": txid_of(tx15),
        "signing_domain": domain,
        "digest_hex": fixture_digest(tx15, [locks[15]], domain).hex(),
        "prev_lock": locks[15].hex(),
    }

    # 16 unknown opcode: 锁字节 0x62 解码期未知
    tx16 = spend_tx(16, assemble([b"\x01"]))
    cases["16"] = {
        "label": labels[16], "expected": "UNKNOWN_OPCODE",
        "reason": "锁定脚本含白名单外字节 0x62，解码期即拒绝",
        "prev_vout": 16, "tx": tx16.wire(), "txid": txid_of(tx16),
        "signing_domain": domain,
        "digest_hex": fixture_digest(tx16, [locks[16]], domain).hex(),
        "prev_lock": locks[16].hex(),
    }

    # 17 unclean: unlock 空，锁 OP_1 OP_2 → 两个元素
    tx17 = spend_tx(17, b"")
    cases["17"] = {
        "label": labels[17], "expected": "UNCLEAN_STACK",
        "reason": "脚本结束栈上有 2 个元素，违反唯一真元素的干净栈规则",
        "prev_vout": 17, "tx": tx17.wire(), "txid": txid_of(tx17),
        "signing_domain": domain,
        "digest_hex": fixture_digest(tx17, [locks[17]], domain).hex(),
        "prev_lock": locks[17].hex(),
    }

    # 18 IF true: unlock 压 OP_1
    finalize("18", labels[18], "OK",
             "条件为真走 IF 分支留下 1；ELSE 中的 RETURN 在非活跃分支不执行",
             locks[18], lambda d: assemble([1]))

    # 19 IF false: unlock 压 OP_0
    finalize("19", labels[19], "OK",
             "条件为假走 ELSE 分支留下 1；IF 分支中的 RETURN 在非活跃分支不执行",
             locks[19], lambda d: assemble([0]))

    # 20 service demo ok
    finalize("20", labels[20], "OK",
             "服务正常路径演示：合法 P2PK 花费；再次提交应得 TX_ALREADY_ACCEPTED，"
             "双花应得 UTXO_MISSING",
             locks[20], lambda d: assemble([rfc6979_signature(keys["alice"], d)]))

    doc = {
        "note": "用例预期由工具侧手工枚举，签名由独立 ecdsa 库生成；不经过被测 VM",
        "genesis_txid": gtxid,
        "domain_tag": domain,
        "wrong_domain_tag": WRONG_DOMAIN,
        "out_value": OUT_VALUE,
        "receive_lock": receive_lock.hex(),
        "cases": cases,
    }
    (FIXTURES / "cases.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True), "utf-8")

    # ---- 交叉一致性断言：工具侧摘要必须与 stackvm.sighash 一致 ----
    from stackvm.sighash import signature_digest
    from stackvm.transaction import transaction_from_dict
    for cid, case in cases.items():
        t = transaction_from_dict(case["tx"])
        core_d = signature_digest(t, [bytes.fromhex(case["prev_lock"])],
                                  case["signing_domain"])
        assert core_d.hex() == case["digest_hex"], (
            f"夹具 {cid} 的摘要在工具与核心间不一致：{core_d.hex()} != {case['digest_hex']}")

    print(f"已生成夹具：{FIXTURES}")
    print(f"genesis txid = {gtxid}")
    print(f"用例数 = {len(cases)}；域标签 = {domain}")


if __name__ == "__main__":
    main()
