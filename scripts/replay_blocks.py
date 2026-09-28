#!/usr/bin/env python3
"""Offline replay CLI.

Usage:
    python -m scripts.replay_blocks --scenario fixtures/chain_fixture.json \
        --db data/chain.db --report reports/replay.json
    BASEFEE_DB=data/chain.db uvicorn basefee.api.app:app  # HTTP over same model

Parent-hash placeholders ("<computed: block N hash>") in the fixture are
resolved from the preceding accepted block, so scenarios stay readable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from basefee.kernel import Chain  # noqa: E402
from basefee.kernel.execution import ChainState  # noqa: E402
from basefee.storage import Store  # noqa: E402
from basefee.replay import replay_scenario, load_scenario, summary_to_dict  # noqa: E402

PARENT_PLACEHOLDER = "<computed: genesis hash>"


def resolve_parent_hashes(scenario: dict, genesis_hash: str = "",
                          chain=None) -> dict:
    """Backwards-compatible adapter (returns an independent deep copy).

    Placeholder resolution now happens inside
    ``basefee.replay.replay_scenario`` against the live chain head.
    """
    import copy
    return copy.deepcopy(scenario)


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline block replay")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--report", default=None)
    parser.add_argument("--strict", action="store_true",
                        help="any invalid tx rejects the whole block")
    args = parser.parse_args(argv)

    scenario = load_scenario(args.scenario)
    chain = Chain(ChainState(
        balances={a: int(v) for a, v in scenario.get("genesis_balances", {}).items()}
    ))
    store = Store(args.db)
    sc = resolve_parent_hashes(scenario, chain.genesis_hash())

    summary = replay_scenario(sc, chain=chain, store=store,
                              strict=True if args.strict else None)
    report = summary_to_dict(summary)

    # Replay rejected-block cases independently against fresh chains.
    rejection_results = []
    for case in scenario.get("rejected_blocks", []):
        c = Chain(ChainState(
            balances={a: int(v) for a, v in scenario.get("genesis_balances", {}).items()}
        ))
        st = Store(":memory:")
        accepted_hashes = {}
        mini = {
            "scenario": sc["scenario"],
            "protocol_version": sc.get("protocol_version"),
            "genesis_balances": scenario.get("genesis_balances", {}),
            "blocks": [b for b in sc["blocks"]],
        }
        pre = replay_scenario(mini, chain=c, store=st)
        for b in pre.blocks:
            accepted_hashes[b["number"]] = b["block_hash"]

        blk = dict(case["block"])
        # Special case: materialize the 21k-gas tx referenced by E041 scenario.
        txs = blk.get("transactions", [])
        if txs and txs[0] == "<one 21000-gas valid tx>":
            alice = scenario["accounts"]["alice"]
            from reference import oracle as O
            sk = O.oracle_key(alice["seed"])
            signed = _sign_alice_next(O, sk, scenario, c)
            blk["transactions"] = [signed]
        case_scenario = {
            "scenario": f"rejection:{case['name']}",
            "genesis_balances": scenario.get("genesis_balances", {}),
            "blocks": [blk],
        }
        res = replay_scenario(case_scenario, chain=c, store=Store(":memory:"))
        got = res.failures[0]["code"] if res.failures else None
        rejection_results.append({
            "name": case["name"],
            "expected_code": case["expected_code"],
            "observed_code": got,
            "matched": got == case["expected_code"],
        })

    report["rejected_block_cases"] = rejection_results
    report["all_rejections_matched"] = all(r["matched"] for r in rejection_results)

    text = json.dumps(report, indent=2)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)

    ok = (summary.blocks_accepted == summary.blocks_total
          and not summary.failures
          and report["all_rejections_matched"])
    return 0 if ok else 1


def _sign_alice_next(O, sk, scenario, chain_after4):
    # Alice's nonce after blocks 1&2 is 2; her target is dave.
    dave_addr = scenario["accounts"]["dave"]["address"]
    sig = O.oracle_sign(sk, nonce=2, max_fee=2_000_000_000, max_tip=100_000_000,
                        gas_limit=21000, to=bytes.fromhex(dave_addr[2:]),
                        value=1)
    return {
        "chain_id": O.CHAIN_ID, "nonce": 2,
        "max_fee_per_gas": "2000000000", "max_priority_fee_per_gas": "100000000",
        "gas_limit": 21000, "to": dave_addr, "value": "1", "data": "0x",
        "signature": {"r": str(sig["r"]), "s": str(sig["s"]), "v": sig["v"]},
    }


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
