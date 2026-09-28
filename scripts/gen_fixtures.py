#!/usr/bin/env python3
"""生成全部本地合成夹具（fixtures/*.json）。

关键原则：夹具中的 expected_* 由独立参考实现 reference/oracle.py 计算，
**不经过 utxo_ledger 内核**；脚本只是构造器(fab) + oracle 的组合层。

用法：python scripts/gen_fixtures.py [--out fixtures]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from utxo_ledger import encoding, fab  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "independent_oracle", os.path.join(ROOT, "reference", "oracle.py")
)
oracle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oracle)


def _bj(block) -> dict:
    return encoding.block_to_json(block)


def _build_base():
    """共享底座：genesis 发行给 K0/K1，高度 1 再做一笔转账并留手续费。"""
    ring = fab.KeyRing()
    k0, k1, k2 = ring.pub(0), ring.pub(1), ring.pub(2)

    # genesis：K0 得 1000，K1 得 500
    g = fab.genesis_block(
        [fab.issue_tx([(1000, k0), (500, k1)])]
    )

    # 高度 1：K0 花费 g:0(1000) -> K2 收 900，费 100；K1 花费 g:1(500) -> K0 收 500
    g_ids = [encoding.txid_of(t) for t in g.transactions]
    t1 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_ids[0], 0)],
            [fab.make_output(900, k2)],
            fee=100,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    t2 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_ids[0], 1)],
            [fab.make_output(500, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(1)],
    )
    b1 = fab.next_block([t1, t2], g)
    return ring, g, b1, {"k0": k0, "k1": k1, "k2": k2}


def _case_from_block(name: str, block, ostate, *, note: str) -> dict:
    raw = _bj(block)
    return _case_from_raw(name, raw, ostate, note=note)


def _case_from_raw(name: str, raw: dict, ostate, *, note: str) -> dict:
    verdict = oracle.evaluate_block(raw, ostate)
    case: dict = {
        "name": name,
        "description": note,
        "block": raw,
        "expected_accepted": verdict["accepted"],
    }
    if not verdict["accepted"]:
        case["expected_category"] = verdict["category"]
        case["expected_code"] = verdict["code"]
        if verdict["tx_index"] is not None:
            case["expected_tx_index"] = verdict["tx_index"]
    case["oracle_per_tx"] = verdict["per_tx"]
    return case


def build_all() -> tuple[dict, list[dict]]:
    ring, g, b1, pubs = _build_base()
    prefix_raw = [_bj(g), _bj(b1)]

    # oracle 参考状态推进到高度 1
    ostate = oracle.genesis_state()
    for raw in prefix_raw:
        v = oracle.evaluate_block(raw, ostate)
        assert v["accepted"], f"底座块必须被 oracle 接受: {v}"
        ostate = oracle.apply_block(raw, ostate, v)

    g_ids = [encoding.txid_of(t) for t in g.transactions]
    b1_ids = [encoding.txid_of(t) for t in b1.transactions]
    k0, k1, k2 = pubs["k0"], pubs["k1"], pubs["k2"]

    cases: list[dict] = []

    # --- 1. 有效转账（含费用，链上输入） --------------------------------
    good = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],  # 900 -> K2
            [fab.make_output(800, k0), fab.make_output(95, k1)],
            fee=5,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    cases.append(
        _case_from_block(
            "valid_transfer_with_fee",
            fab.next_block([good], b1),
            ostate,
            note="K2 花 900，输出 800+95，费 5，守恒",
        )
    )

    # --- 2. 块内前序引用合法（同一区块内两笔串联） ----------------------
    a = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],  # 900 K2
            [fab.make_output(700, k0), fab.make_output(190, k2)],
            fee=10,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    a_id = encoding.txid_of(a)
    b = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(a_id, 1)],  # 190 K2
            [fab.make_output(190, k1)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    cases.append(
        _case_from_block(
            "valid_intra_block_reference",
            fab.next_block([a, b], b1),
            ostate,
            note="第二笔引用同块第一笔的输出（前序引用允许）",
        )
    )

    # --- 3. 块内双花：两笔 tx 花同一个链上 UTXO -------------------------
    ds1 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    ds2 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, k1)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    cases.append(
        _case_from_block(
            "intra_block_double_spend",
            fab.next_block([ds1, ds2], b1),
            ostate,
            note="同块两笔交易先后花费同一 UTXO，第二笔必须拒绝",
        )
    )

    # --- 4. 同交易重复输入 ----------------------------------------------
    dup = fab.sign_tx(
        fab.unsigned_tx(
            [
                encoding.Outpoint(b1_ids[0], 0),
                encoding.Outpoint(b1_ids[0], 0),
            ],
            [fab.make_output(1800, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2), ring.priv(2)],
    )
    cases.append(
        _case_from_block(
            "duplicate_input_same_tx",
            fab.next_block([dup], b1),
            ostate,
            note="单笔交易两次引用同一 outpoint",
        )
    )

    # --- 5. 零值输出 ----------------------------------------------------
    zero = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],  # 900
            [fab.make_output(900, k0), fab.make_output(0, k1)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    cases.append(
        _case_from_block(
            "zero_value_output",
            fab.next_block([zero], b1),
            ostate,
            note="输出含 0 金额，必须在验签前即拒绝",
        )
    )

    # --- 6. 签名篡改：正确签名后翻转一个字节 ----------------------------
    tamper_base = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, k1)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    bad_sig = bytearray(tamper_base.witnesses[0].signature)
    bad_sig[-1] ^= 0x01
    tampered = fab.with_witnesses(tamper_base, [bytes(bad_sig)])
    cases.append(
        _case_from_block(
            "signature_tampered",
            fab.next_block([tampered], b1),
            ostate,
            note="合法签名最后一字节被翻转，验签必须失败",
        )
    )

    # --- 7. 前向引用：index0 交易引用 index1 交易的输出 ----------------
    fwd1 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    fwd1_id = encoding.txid_of(fwd1)
    fwd0 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 1), encoding.Outpoint(fwd1_id, 0)],
            [fab.make_output(500, k1), fab.make_output(900, k2)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0), ring.priv(2)],
    )
    # 顺序 [fwd0, fwd1]：fwd0 引用 fwd1 => 前向引用
    cases.append(
        _case_from_block(
            "forward_reference",
            fab.next_block([fwd0, fwd1], b1),
            ostate,
            note="index0 交易引用 index1 交易的输出",
        )
    )

    # --- 8. 块高度不连续（跳过高度 2 直接提交高度 3） -------------------
    gap_tx = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    gap_block = fab.make_block(
        [gap_tx],
        height=3,  # tip=1，期望 2
        prev_hash=encoding.block_id_of(b1),
    )
    cases.append(
        _case_from_block(
            "block_height_gap",
            gap_block,
            ostate,
            note="tip=1 时提交 height=3，必须报 BLOCK_CONFLICT",
        )
    )

    # --- 9. 历史双花：再次花费 b1:0（已花费） ---------------------------
    hist = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(g_ids[0], 0)],  # g:0 在 b1 已被 t1 花费
            [fab.make_output(1000, k1)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(0)],
    )
    cases.append(
        _case_from_block(
            "spend_already_spent_historical",
            fab.next_block([hist], b1),
            ostate,
            note="引用在历史块中已花费的 outpoint",
        )
    )

    # --- 10. 未知 outpoint ----------------------------------------------
    unknown = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b"\x11" * 32, 0)],
            [fab.make_output(1, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    cases.append(
        _case_from_block(
            "unknown_outpoint",
            fab.next_block([unknown], b1),
            ostate,
            note="引用任何地方都不存在的 txid",
        )
    )

    # --- 11. 价值不守恒（少输出） ---------------------------------------
    cons = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],  # 900
            [fab.make_output(800, k0)],  # 费应为100，这里报0
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    cases.append(
        _case_from_block(
        "conservation_mismatch",
        fab.next_block([cons], b1),
        ostate,
        note="900 输入 vs 800 输出 + 0 费",
        )
    )

    # --- 12. 非 genesis 发行 --------------------------------------------
    issue = fab.issue_tx([(100, k0)])
    cases.append(
        _case_from_block(
            "illegal_issuance_non_genesis",
            fab.next_block([issue], b1),
            ostate,
            note="高度>=1 的块里出现无输入交易",
        )
    )

    # --- 13. 资源上限：超小 max_txs_per_block（由测试注入 limits，
    #        夹具默认只放一个正常 case；此处用字节超限构造：不生成，
    #        改由单元测试直接断言；为夹具完整性放一个见证数不符用例） ---
    witbad = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    # 多加一条见证
    witbad = encoding.Transaction(
        version=witbad.version,
        inputs=witbad.inputs,
        outputs=witbad.outputs,
        fee=witbad.fee,
        witnesses=witbad.witnesses
        + (encoding.Witness(signature=b"\x30\x06\x02\x01\x01\x02\x01\x01"),),
    )
    cases.append(
        _case_from_block(
            "witness_count_mismatch",
            fab.next_block([witbad], b1),
            ostate,
            note="见证数比输入数多 1",
        )
    )

    # --- 14. 根不匹配（篡改 tx_root） -----------------------------------
    good2 = fab.sign_tx(
        fab.unsigned_tx(
            [encoding.Outpoint(b1_ids[0], 0)],
            [fab.make_output(900, k0)],
            fee=0,
        ),
        owner_privkeys=[ring.priv(2)],
    )
    bad_root_block = fab.make_block(
        [good2],
        height=b1.header.height + 1,
        prev_hash=encoding.block_id_of(b1),
        tx_root_override=b"\x22" * 32,
    )
    cases.append(
        _case_from_block(
            "tx_root_mismatch",
            bad_root_block,
            ostate,
            note="块头 tx_root 与交易列表不一致",
        )
    )

    keys_fixture = {
        "schema_version": 1,
        "name": "synthetic-keypairs",
        "description": "确定性本地测试密钥（仅合成资产，无生产身份）",
        "keys": [
            {
                "index": i,
                "pubkey_compressed_hex": ring.pub(i).hex(),
            }
            for i in range(5)
        ],
    }
    main_fixture = {
        "schema_version": 1,
        "name": "utxo-validation-cases",
        "description": "UTXO 账本验证夹具：底座两块 + 14 用例；预期由独立 oracle 生成",
        "prefix_blocks": prefix_raw,
        "cases": cases,
    }
    return keys_fixture, [main_fixture]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "fixtures"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    keys_fixture, fixtures = build_all()
    with open(os.path.join(args.out, "keys.json"), "w", encoding="utf-8") as fh:
        json.dump(keys_fixture, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    for i, fx in enumerate(fixtures):
        name = "validation_cases.json" if i == 0 else f"{fx['name']}.json"
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as fh:
            json.dump(fx, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        n_accept = sum(1 for c in fx["cases"] if c["expected_accepted"])
        print(
            f"写入 {name}: {len(fx['prefix_blocks'])} 前置块, "
            f"{len(fx['cases'])} 用例 ({n_accept} 应接受 / "
            f"{len(fx['cases']) - n_accept} 应拒绝)"
        )
    print(f"写入 keys.json: {len(keys_fixture['keys'])} 个合成密钥")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
