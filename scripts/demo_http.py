#!/usr/bin/env python3
"""端到端示例：对**正在运行的** HTTP 服务发起完整流程。

用法::

    python -m local_txpool.cli serve --port 8000      # 终端 A
    python scripts/demo_http.py                         # 终端 B

脚本只依赖公开 API：合成账户、签名 RLP 原始交易、提交、查候选、出块、
确认/回滚、按 request_id 拉审计。所有密钥本地随机/确定性生成，无生产数据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许直接 `python scripts/demo_http.py` 运行（无需手动设置 PYTHONPATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from local_txpool.core import crypto

BASE = "http://127.0.0.1:8077"
GWEI = 1_000_000_000


def pp(title: str, obj) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:2_000])


def main() -> int:
    # 1) 两个确定性合成账户（仅本地；生产环境绝不要用这种派生）
    from eth_hash.auto import keccak

    pk_a = keccak(b"demo/alice")
    pk_b = keccak(b"demo/bob")
    addr_a = crypto.address_for_private_key(pk_a)
    addr_b = crypto.address_for_private_key(pk_b)

    with httpx.Client(base_url=BASE, timeout=10) as http:
        base = http.get("/health")
        base.raise_for_status()
        chain_id = int(base.json()["chain_id"])
        pp("health", base.json())

        # 2) 注资
        for addr in (addr_a, addr_b):
            r = http.post(
                "/accounts",
                json={"address": addr, "balance": 10**18},
            )
            r.raise_for_status()

        def signed(nonce: int, gas_price: int, pk: bytes) -> str:
            tx = crypto.sign_transaction(
                private_key=pk,
                nonce=nonce,
                gas_price=gas_price,
                gas_limit=21_000,
                to="0x" + "22" * 20,
                value=1_000,
                chain_id=chain_id,
            )
            return "0x" + tx.raw.hex()

        # 3) alice：nonce 0 低价 + nonce 2 极高价（缺口）；bob：nonce 0 中价
        submissions = [
            ("alice nonce0  2 gwei", signed(0, 2 * GWEI, pk_a)),
            ("alice nonce2 99 gwei (缺口)", signed(2, 99 * GWEI, pk_a)),
            ("bob   nonce0 20 gwei", signed(0, 20 * GWEI, pk_b)),
        ]
        for label, raw in submissions:
            r = http.post("/transactions", json={"raw_tx": raw})
            print(f"submit {label}: HTTP {r.status_code} "
                  f"request_id={r.json().get('request_id')}")

        # 4) 候选预览：99 gwei 的 a2 必须被 nonce 缺口挡在后面
        pp("candidate (高费不能跨缺口)", http.get("/candidate").json())
        pp("pool", http.get("/transactions/pool").json())

        # 5) 出块
        r = http.post("/blocks/propose")
        pp("proposed block", r.json())
        rid = r.json()["request_id"]

        # 6) 按请求身份拉完整处理轨迹（移入/移出理由、版本）
        pp(f"audit trail for {rid}",
            http.get(f"/audit/requests/{rid}").json())

        # 7) 回滚（默认 depth=3，当前高度 1 可回滚）
        pp("rollback to 0",
            http.post("/blocks/rollback",
                      json={"target_number": 0}).json())
        pp("pool after rollback", http.get("/transactions/pool").json())

    print("\n演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
