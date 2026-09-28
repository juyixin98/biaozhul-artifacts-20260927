"""Generate the deterministic JSONL fixture-case file used by offline replay.

Run::

    .venv/bin/python scripts/build_fixtures.py

It writes ``fixtures/cases.jsonl`` -- one self-contained JSON object per line:

    {
      "name": "...",
      "setup_blocks": ["<hex block>", ...],
      "block_under_test": "<hex block>",
      "expect": {"accepted": false, "code": "double_spend", "category": "state"}
    }

The expected ``code``/``category`` values are written from the written
specification and are additionally cross-checked at generation time against the
independent oracle, so the committed file is not merely "whatever the core
returned".
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utxo_ledger.encoding import Block, Outpoint, Transaction, TxInput, TxOutput, encode_block, tx_sighash  # noqa: E402

from tests.fixtures import FixtureBuilder, coinbase_tx, named_key, transfer_tx  # noqa: E402
from tests import oracle  # noqa: E402


def _oracle_code(setup_hex, target_hex):
    st = oracle.OracleState()
    for hx in setup_hex:
        oracle.apply_block_to_state(bytes.fromhex(hx), st)
    ref = st.copy()
    try:
        oracle.apply_block_to_state(bytes.fromhex(target_hex), ref)
        return None
    except oracle.OracleError as exc:
        return exc.code, exc.category


def build_cases():
    cases = []

    # 1. Valid single-tx genesis-style block.
    fb = FixtureBuilder()
    alice = named_key("alice")
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    cases.append(
        {
            "name": "valid_genesis_coinbase",
            "setup_blocks": [],
            "block_under_test": encode_block(g).hex(),
            "expect": {"accepted": True},
        }
    )

    # 2. Valid multi-tx block with a fee + intra-block back reference.
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    bob, carol = named_key("bob"), named_key("carol")
    tx1 = transfer_tx(
        [(Outpoint(g.transactions[0].txid, 0), alice.public_bytes)],
        [(100_000, bob.public_bytes), (899_000, alice.public_bytes)],
        {0: alice},
    )
    tx2 = transfer_tx(
        [(Outpoint(tx1.txid, 0), bob.public_bytes)],
        [(99_500, carol.public_bytes)],
        {0: bob},
    )
    target = Block(
        1, 2, fb.prev_hash,
        (coinbase_tx(2, [(1_001_500, alice.public_bytes)]), tx1, tx2),
    )
    cases.append(
        {
            "name": "valid_multi_tx_intra_block_chain",
            "setup_blocks": [encode_block(g).hex()],
            "block_under_test": encode_block(target).hex(),
            "expect": {"accepted": True, "fee_total": 1500},
        }
    )

    # 3. Intra-block double spend (two transactions, same committed outpoint).
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    src = Outpoint(g.transactions[0].txid, 0)
    d1 = transfer_tx(
        [(src, alice.public_bytes)],
        [(400_000, bob.public_bytes), (599_000, alice.public_bytes)],
        {0: alice},
    )
    d2 = transfer_tx(
        [(src, alice.public_bytes)],
        [(400_000, carol.public_bytes), (599_000, alice.public_bytes)],
        {0: alice},
    )
    target = Block(
        1, 2, fb.prev_hash,
        (coinbase_tx(2, [(1_000_000, alice.public_bytes)]), d1, d2),
    )
    cases.append(
        {
            "name": "reject_intra_block_double_spend",
            "setup_blocks": [encode_block(g).hex()],
            "block_under_test": encode_block(target).hex(),
            "expect": {
                "accepted": False,
                "code": "double_spend",
                "category": "state",
            },
        }
    )

    # 4. Duplicate input within one transaction.
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    src = Outpoint(g.transactions[0].txid, 0)
    unsigned = Transaction(
        1,
        (TxInput(src, b""), TxInput(src, b"")),
        (TxOutput(999_000, bob.public_bytes),),
    )
    sig = alice.sign(tx_sighash(unsigned))
    dup = Transaction(
        1,
        (TxInput(src, sig), TxInput(src, sig)),
        unsigned.outputs,
    )
    target = Block(
        1, 2, fb.prev_hash,
        (coinbase_tx(2, [(1_001_000, alice.public_bytes)]), dup),
    )
    cases.append(
        {
            "name": "reject_duplicate_input_same_tx",
            "setup_blocks": [encode_block(g).hex()],
            "block_under_test": encode_block(target).hex(),
            "expect": {
                "accepted": False,
                "code": "duplicate_input",
                "category": "state",
            },
        }
    )

    # 5. Zero-value output.
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    src = Outpoint(g.transactions[0].txid, 0)
    z = transfer_tx(
        [(src, alice.public_bytes)],
        [(0, bob.public_bytes), (999_000, alice.public_bytes)],
        {0: alice},
    )
    target = Block(
        1, 2, fb.prev_hash,
        (coinbase_tx(2, [(1_001_000, alice.public_bytes)]), z),
    )
    cases.append(
        {
            "name": "reject_zero_value_output",
            "setup_blocks": [encode_block(g).hex()],
            "block_under_test": encode_block(target).hex(),
            "expect": {
                "accepted": False,
                "code": "zero_value_output",
                "category": "input",
            },
        }
    )

    # 6. Signature tamper (wrong signer).
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    attacker = named_key("attacker")
    src = Outpoint(g.transactions[0].txid, 0)
    st = transfer_tx(
        [(src, alice.public_bytes)],
        [(400_000, bob.public_bytes), (599_000, alice.public_bytes)],
        {0: attacker},
    )
    target = Block(
        1, 2, fb.prev_hash,
        (coinbase_tx(2, [(1_001_000, alice.public_bytes)]), st),
    )
    cases.append(
        {
            "name": "reject_signature_tampered",
            "setup_blocks": [encode_block(g).hex()],
            "block_under_test": encode_block(target).hex(),
            "expect": {
                "accepted": False,
                "code": "sig_tampered",
                "category": "state",
            },
        }
    )

    # 7. Forward reference.
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    src = Outpoint(g.transactions[0].txid, 0)
    fund = transfer_tx(
        [(src, alice.public_bytes)],
        [(100_000, bob.public_bytes), (899_000, alice.public_bytes)],
        {0: alice},
    )
    early = transfer_tx(
        [(Outpoint(fund.txid, 0), bob.public_bytes)],
        [(99_000, carol.public_bytes)],
        {0: bob},
    )
    target = Block(
        1, 2, fb.prev_hash,
        (coinbase_tx(2, [(1_000_000, alice.public_bytes)]), early, fund),
    )
    cases.append(
        {
            "name": "reject_forward_reference",
            "setup_blocks": [encode_block(g).hex()],
            "block_under_test": encode_block(target).hex(),
            "expect": {
                "accepted": False,
                "code": "forward_reference",
                "category": "state",
            },
        }
    )

    # 8. Conservation violation (outputs exceed inputs).
    fb = FixtureBuilder()
    g = fb.append([coinbase_tx(1, [(1_000_000, alice.public_bytes)])])
    src = Outpoint(g.transactions[0].txid, 0)
    over = transfer_tx(
        [(src, alice.public_bytes)],
        [(1_000_001, carol.public_bytes)],
        {0: alice},
    )
    target = Block(
        1, 2, fb.prev_hash,
        (coinbase_tx(2, [(1_000_000, alice.public_bytes)]), over),
    )
    cases.append(
        {
            "name": "reject_outputs_exceed_inputs",
            "setup_blocks": [encode_block(g).hex()],
            "block_under_test": encode_block(target).hex(),
            "expect": {
                "accepted": False,
                "code": "fee_negative",
                "category": "state",
            },
        }
    )

    # Cross-check every expectation against the independent oracle.
    for case in cases:
        exp = case["expect"]
        oc = _oracle_code(case["setup_blocks"], case["block_under_test"])
        if exp["accepted"]:
            assert oc is None, f"{case['name']}: oracle rejected a case expected valid"
        else:
            assert oc is not None, f"{case['name']}: oracle accepted a case expected invalid"
            assert oc[0] == exp["code"], (
                f"{case['name']}: oracle code {oc[0]} != {exp['code']}"
            )
            assert oc[1] == exp["category"], (
                f"{case['name']}: oracle category {oc[1]} != {exp['category']}"
            )
    return cases


def main():
    cases = build_cases()
    out_dir = os.path.join(ROOT, "fixtures")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "cases.jsonl")
    with open(out_path, "w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, sort_keys=True) + "\n")
    print(f"wrote {len(cases)} cases -> {out_path}")


if __name__ == "__main__":
    main()
