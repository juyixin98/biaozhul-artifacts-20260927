#!/usr/bin/env python3
"""Build the committed synthetic chain fixture using ONLY the reference oracle.

Run:  python scripts/build_fixtures.py
Out:  fixtures/chain_fixture.json

The fixture contains real signatures (secp256k1, RFC6979) over canonical RLP,
derived from fixed seed keys. Expected economics and next-base-fees are computed
by the independent oracle (``reference/oracle.py``); the production kernel is
not imported anywhere in this script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference import oracle as O  # noqa: E402

GENESIS_HASH = "0x" + "00" * 32
# Bob funds several cheap transfers but never a 30m-gas block-filling tx.
BOB_BALANCE = 50_000_000_000_000
# Eve is funded with 1 ether; at ~1 gwei base fee her 21k tx needs ~21e12 +
# value, so a value transfer of 0.9 ether is unaffordable -> E033.
EVE_BALANCE = 1_000_000_000_000_000_000


def _addr(sk) -> str:
    return O.oracle_address(O.oracle_pub_raw(sk))


def _tx(sk, *, nonce, max_fee, max_tip, gas, to, value, data=b""):
    sig = O.oracle_sign(sk, nonce=nonce, max_fee=max_fee, max_tip=max_tip,
                        gas_limit=gas, to=bytes.fromhex(to[2:]), value=value,
                        data=data)
    sender = _addr(sk)
    return {
        "chain_id": O.CHAIN_ID,
        "nonce": nonce,
        "max_fee_per_gas": str(max_fee),
        "max_priority_fee_per_gas": str(max_tip),
        "gas_limit": gas,
        "to": to,
        "value": str(value),
        "data": "0x" + data.hex(),
        "signature": {"r": str(sig["r"]), "s": str(sig["s"]), "v": sig["v"]},
        "_meta": {"sender": sender, "digest": sig["digest"]},
    }


def build() -> dict:
    alice = O.oracle_key(1)
    bob = O.oracle_key(2)
    carol = O.oracle_key(3)
    dave = O.oracle_key(4)
    eve = O.oracle_key(5)
    alice_addr, bob_addr = _addr(alice), _addr(bob)
    carol_addr, dave_addr = _addr(carol), _addr(dave)
    eve_addr = _addr(eve)

    gas_limit = O.GENESIS_GAS_LIMIT
    genesis_base = O.GENESIS_BASE_FEE
    b1_base = O.oracle_next_base_fee(genesis_base, 21000, gas_limit)
    b2_base = O.oracle_next_base_fee(b1_base, 30_000_000, gas_limit)
    b3_base = O.oracle_next_base_fee(b2_base, 0, gas_limit)

    # ---- block 1: one valid tx, two skipped-invalid txs; gas_used = 21000 ----
    t1 = _tx(alice, nonce=0, max_fee=2_000_000_000, max_tip=100_000_000,
             gas=21000, to=bob_addr, value=1_000_000_000_000)
    t1_bad_fee = _tx(bob, nonce=0, max_fee=1, max_tip=1, gas=21000,
                     to=alice_addr, value=1)  # E020 (max_fee < base)
    t1_bad_sig = _tx(carol, nonce=0, max_fee=2_000_000_000, max_tip=1,
                     gas=21000, to=dave_addr, value=1)
    t1_bad_sig["signature"]["r"] = str(12345)  # corrupt signature -> E011

    # ---- block 2: full block, one 30m-gas tx ----
    t2 = _tx(alice, nonce=1, max_fee=2_000_000_000, max_tip=1_000_000_000,
             gas=30_000_000, to=carol_addr, value=0)
    # Eve can pay the gas for a 21k tx but not gas + 2 ether value -> E033
    t2_broke = _tx(eve, nonce=0, max_fee=2_000_000_000, max_tip=1_000_000_000,
                   gas=21000, to=carol_addr, value=2_000_000_000_000_000_000)

    # ---- block 3: empty ----
    # ---- block 4: valid cheap tx at low base fee; a bad nonce tx ----
    t4 = _tx(bob, nonce=0, max_fee=2_000_000_000, max_tip=100_000_000,
             gas=21000, to=dave_addr, value=1)
    t4_nonce = _tx(alice, nonce=99, max_fee=2_000_000_000, max_tip=1,
                   gas=21000, to=dave_addr, value=1)  # E031

    def economics_for(tx, base, gas, balance, expected_nonce):
        return O.oracle_tx_economics(
            base_fee=base, gas_limit=gas,
            max_fee=int(tx["max_fee_per_gas"]),
            max_priority=int(tx["max_priority_fee_per_gas"]),
            value=int(tx["value"]), balance=balance, nonce=tx["nonce"],
            expected_nonce=expected_nonce,
        )

    e1 = economics_for(t1, genesis_base, 21000, 10**24, 0)
    e2 = economics_for(t2, b1_base, 30_000_000, 10**24 - e1["total_cost"], 1)
    # alice balance after block1: 10^24 - e1 total; block2 full 30m tx
    e4 = economics_for(t4, b3_base, 21000, BOB_BALANCE, 0)

    scenario = {
        "scenario": "synthetic_chain_alpha",
        "protocol_version": json.loads(
            (ROOT / "config" / "protocol.json").read_text())["protocol_version"],
        "description": "Deterministic offline chain: target/empty/full blocks, "
                       "fee-cap failures, bad signature, bad nonce, insufficient "
                       "balance and hard block-level rejections.",
        "genesis": {
            "number": 0,
            "block_hash": GENESIS_HASH,
            "base_fee_per_gas": str(genesis_base),
            "gas_limit": gas_limit,
            "gas_used": 0,
        },
        "accounts": {
            "alice": {"address": alice_addr, "seed": 1,
                      "genesis_balance": str(10**24), "genesis_nonce": 0},
            "bob": {"address": bob_addr, "seed": 2,
                    "genesis_balance": str(BOB_BALANCE), "genesis_nonce": 0},
            "carol": {"address": carol_addr, "seed": 3,
                      "genesis_balance": "0", "genesis_nonce": 0},
            "dave": {"address": dave_addr, "seed": 4,
                     "genesis_balance": "0", "genesis_nonce": 0},
            "eve": {"address": eve_addr, "seed": 5,
                    "genesis_balance": str(EVE_BALANCE), "genesis_nonce": 0},
        },
        "genesis_balances": {
            alice_addr: str(10**24),
            bob_addr: str(BOB_BALANCE),
            carol_addr: "0",
            dave_addr: "0",
            eve_addr: str(EVE_BALANCE),
        },
        "genesis_nonces": {alice_addr: 0, bob_addr: 0, carol_addr: 0,
                           dave_addr: 0, eve_addr: 0},
        "blocks": [
            {
                "number": 1, "parent_hash": GENESIS_HASH,
                "base_fee_per_gas": str(genesis_base),
                "gas_limit": gas_limit, "gas_used": 21000,
                "transactions": [t1, t1_bad_fee, t1_bad_sig],
                "expected": {
                    "accepted": True,
                    "valid_count": 1,
                    "invalid": [
                        {"index": 1, "code": "E020_MAX_FEE_BELOW_BASE"},
                        {"index": 2, "code": "E011_SIGNATURE_INVALID"},
                    ],
                    "next_base_fee": str(b1_base),
                    "burned": str(e1["burned"]),
                    "tipped": str(e1["tipped"]),
                    "price": str(e1["price"]),
                    "tip": str(e1["tip"]),
                },
            },
            {
                "number": 2,
                "parent_hash": "<computed: block 1 hash>",
                "base_fee_per_gas": str(b1_base),
                "gas_limit": gas_limit, "gas_used": 30_000_000,
                "transactions": [t2, t2_broke],
                "expected": {
                    "accepted": True,
                    "valid_count": 1,
                    "invalid": [
                        {"index": 1, "code": "E033_INSUFFICIENT_BALANCE"},
                    ],
                    "next_base_fee": str(b2_base),
                    "burned": str(e2["burned"]),
                    "tipped": str(e2["tipped"]),
                    "price": str(e2["price"]),
                    "tip": str(e2["tip"]),
                },
            },
            {
                "number": 3,
                "parent_hash": "<computed: block 2 hash>",
                "base_fee_per_gas": str(b2_base),
                "gas_limit": gas_limit, "gas_used": 0,
                "transactions": [],
                "expected": {
                    "accepted": True, "valid_count": 0, "invalid": [],
                    "next_base_fee": str(b3_base),
                    "burned": "0", "tipped": "0",
                },
            },
            {
                "number": 4,
                "parent_hash": "<computed: block 3 hash>",
                "base_fee_per_gas": str(b3_base),
                "gas_limit": gas_limit, "gas_used": 21000,
                "transactions": [t4, t4_nonce],
                "expected": {
                    "accepted": True,
                    "valid_count": 1,
                    "invalid": [{"index": 1, "code": "E031_NONCE_MISMATCH"}],
                    "next_base_fee": str(O.oracle_next_base_fee(b3_base, 21000, gas_limit)),
                    "burned": str(e4["burned"]),
                    "tipped": str(e4["tipped"]),
                    "price": str(e4["price"]),
                    "tip": str(e4["tip"]),
                },
            },
        ],
        "rejected_blocks": [
            {
                "name": "gas used exceeds cap",
                "block": {
                    "number": 5,
                    "parent_hash": "<computed: block 4 hash>",
                    "base_fee_per_gas": str(
                        O.oracle_next_base_fee(b3_base, 21000, gas_limit)),
                    "gas_limit": gas_limit,
                    "gas_used": gas_limit + 21000,
                    "transactions": [],
                },
                "expected_code": "E040_BLOCK_GAS_EXCEEDED",
            },
            {
                "name": "declared base fee does not match parent recurrence",
                "block": {
                    "number": 5,
                    "parent_hash": "<computed: block 4 hash>",
                    "base_fee_per_gas": "1",
                    "gas_limit": gas_limit,
                    "gas_used": 0,
                    "transactions": [],
                },
                "expected_code": "E043_BASE_FEE_MISMATCH",
            },
            {
                "name": "gas_used mismatch against valid transaction gas",
                "block": {
                    "number": 5,
                    "parent_hash": "<computed: block 4 hash>",
                    "base_fee_per_gas": str(
                        O.oracle_next_base_fee(b3_base, 21000, gas_limit)),
                    "gas_limit": gas_limit,
                    "gas_used": 42000,
                    "transactions": ["<one 21000-gas valid tx>"],
                },
                "expected_code": "E041_GAS_USED_MISMATCH",
            },
        ],
    }
    return scenario


def main() -> int:
    scenario = build()
    out = ROOT / "fixtures" / "chain_fixture.json"
    out.write_text(json.dumps(scenario, indent=2, sort_keys=False) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    print("blocks:", [(b["number"], b["expected"]["next_base_fee"])
                      for b in scenario["blocks"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
