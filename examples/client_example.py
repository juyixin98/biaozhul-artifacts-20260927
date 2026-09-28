#!/usr/bin/env python3
"""端到端示例：合成密钥、汇编字节码、签名交易，提交给本地服务并查询收据。

全部数据为本地合成夹具，无真实账号。用法：

    python examples/client_example.py                 # 默认 http://127.0.0.1:8000
    TEACHING_CHAIN_BASE=http://127.0.0.1:9000 python examples/client_example.py

脚本依次演示：
1) 成功交易（整数运算 + KV 写入）；
2) 执行失败交易（写后除零）——状态回滚、费用保留；
3) 临界 gas 交易（dry-run 预演 OUT_OF_GAS）；
4) 篡改签名被拒绝；
5) 查询区块、收据与状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from teaching_chain.encoding import KeyPair  # noqa: E402
from teaching_chain.vm import assemble  # noqa: E402

BASE = os.environ.get("TEACHING_CHAIN_BASE", "http://127.0.0.1:8000")
CHAIN = "teaching-chain-local"


def fixture_key(label: str) -> KeyPair:
    seed = hashlib.sha256(f"demo-{label}".encode()).digest()
    return KeyPair.from_seed(seed)


def sign(kp: KeyPair, code_hex: str, nonce: int, gas_limit: int = 100_000) -> dict:
    body = {"chain": CHAIN, "nonce": nonce, "code": code_hex, "gas_limit": gas_limit}
    from teaching_chain.encoding import sign_transaction

    signed = sign_transaction(kp, body)
    signed["pubkey"] = kp.public_bytes().hex()
    return signed


def pretty(title: str, value: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    alice = fixture_key("alice")
    print(f"演示地址(alice): {alice.address_hex()}")

    # 1) 成功：storage[1] = 7*6
    code_ok = assemble(textwrap.dedent("""
        PUSH8 7
        PUSH8 6
        MUL
        PUSH8 1
        SSTORE
        STOP
    """)).hex()
    tx_ok = sign(alice, code_ok, nonce=1)

    # 2) 失败：先写 storage[2]=9，随后除零 => 写入回滚、gas 全耗
    code_fail = assemble(textwrap.dedent("""
        PUSH8 9
        PUSH8 2
        SSTORE
        PUSH8 1
        PUSH8 0
        DIV
        STOP
    """)).hex()
    tx_fail = sign(alice, code_fail, nonce=2)

    with httpx.Client(base_url=BASE, timeout=10) as http:
        r = http.post("/v1/blocks", json={"transactions": [tx_ok, tx_fail]})
        pretty("提交区块（1 成功 + 1 写后除零）", {
            "http_status": r.status_code, **r.json()
        })
        r.raise_for_status()
        block = r.json()

        # 3) 临界 gas：dry-run 一笔费用不够的交易
        code_small = assemble("PUSH8 1\nPUSH8 2\nADD\nSTOP").hex()
        tx_tight = sign(alice, code_small, nonce=3, gas_limit=25)  # intrinsic≈33
        r = http.post("/v1/dry-run", json={"transaction": tx_tight})
        pretty("dry-run 临界 gas（intrinsic 不足）", {
            "http_status": r.status_code, **r.json()
        })

        # 4) 篡改签名 => 422
        tx_bad = dict(sign(alice, assemble("STOP").hex(), nonce=4))
        tx_bad["signature"] = "00" * 64
        r = http.post("/v1/blocks", json={"transactions": [tx_bad]})
        pretty("篡改签名被拒绝", {"http_status": r.status_code, **r.json()})

        # 5) 查询
        r = http.get("/v1/status")
        pretty("链状态（失败交易的写入不应出现）", r.json())
        r = http.get(f"/v1/blocks/{block['block_number']}")
        pretty(f"区块 {block['block_number']} 头", r.json())

    print("\n离线核验：运行  .venv/bin/teaching-chain-replay --db .teaching-chain/index.db")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
