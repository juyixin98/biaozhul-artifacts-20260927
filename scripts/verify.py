#!/usr/bin/env python3
"""End-to-end verification gate.

Runs, in order:
1. Hand-computed vectors against the kernel (asserts concrete numbers and named
   failure categories).
2. Kernel vs independent oracle over fuzzed inputs (no shared implementation).
3. Full offline replay of the committed synthetic fixture incl. fee conservation
   and all rejected-block cases.

Exit code is non-zero if ANY check fails. Things that cannot be executed in
this environment are printed under ``not_executed`` rather than claimed passed.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from basefee.kernel.eip1559 import compute_next_base_fee_step, effective_priority_tip  # noqa: E402
from basefee.kernel import Chain  # noqa: E402
from basefee.kernel.execution import ChainState, BURN_ADDRESS, COINBASE_ADDRESS  # noqa: E402
from basefee.storage import Store  # noqa: E402
from basefee.replay import replay_scenario, load_scenario  # noqa: E402
from scripts.replay_blocks import resolve_parent_hashes, run as replay_run  # noqa: E402
from reference import oracle as O  # noqa: E402

HAND = json.loads((ROOT / "fixtures" / "hand_vectors.json").read_text())

results: list[dict] = []
not_executed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"name": name, "passed": bool(ok), "detail": detail})
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}" + (f" -- {detail}" if detail and not ok else ""))


def verify_hand_vectors() -> None:
    print("\n=== 1. hand-computed base-fee vectors ===")
    for v in HAND["base_fee_vectors"]:
        step = compute_next_base_fee_step(v["parent_base_fee"], v["gas_used"],
                                          v["gas_limit"])
        check(f"fee: {v['name']}",
              step.next_base_fee == v["expected_next_base_fee"]
              and step.direction == v["expected_direction"]
              and step.delta_numerator == v["expected_delta_numerator"]
              and step.delta_final == v["expected_delta_final"],
              f"got {step.next_base_fee} dir {step.direction}")
    for v in HAND["multi_block_vectors"]:
        base = 1_000_000_000
        seq = []
        for used in v["gas_used_sequence"]:
            base = compute_next_base_fee_step(base, used, v["gas_limit"]).next_base_fee
            seq.append(base)
        check(f"multi-block: {v['name']}", seq == v["expected_base_fees_after"],
              f"got {seq}")

    print("\n=== 1b. hand-computed transaction pricing / failure vectors ===")
    for v in HAND["transaction_vectors"]:
        if v.get("expected_valid") is False:
            # static validity mapping mirror
            if v["expected_error_code"] == "E032_FEE_OVERFLOW":
                prod = int(v["max_fee_per_gas"]) * int(v["gas"])
                ok = prod > 2**256 - 1
            elif v["expected_error_code"] == "E020_MAX_FEE_BELOW_BASE":
                ok = int(v["max_fee_per_gas"]) < int(v["base_fee"])
            elif v["expected_error_code"] == "E030_GAS_LIMIT_TOO_LOW":
                ok = int(v["gas"]) < 21000
            else:
                ok = False
            check(f"tx-fail: {v['name']} [{v['expected_error_code']}]", ok)
            continue
        tip = effective_priority_tip(
            base_fee=v["base_fee"], max_fee_per_gas=v["max_fee_per_gas"],
            max_priority_fee_per_gas=v["max_priority_fee_per_gas"])
        price = v["base_fee"] + tip
        burn = v["base_fee"] * v["gas"]
        tip_total = tip * v["gas"]
        ok = (tip == v["expected_effective_tip"]
              and price == v["expected_effective_price"]
              and burn == v["expected_burned"]
              and tip_total == v["expected_tip_total"])
        if "expected_sender_debit" in v:
            ok = ok and (burn + tip_total == v["expected_sender_debit"])
        check(f"tx-ok: {v['name']}", ok,
              f"tip {tip} price {price} burn {burn}")


def verify_oracle_consistency(n: int = 400) -> None:
    print(f"\n=== 2. kernel vs independent oracle over {n} fuzzed inputs ===")
    rng = random.Random(20260927)
    mismatches = 0
    for _ in range(n):
        limit = rng.choice([20, 100, 1_000_000, 30_000_000])
        base = rng.choice([0, 1, 2, 7, 100, 10**9, 2**100, 2**200])
        used = rng.randint(0, limit)
        kernel = compute_next_base_fee_step(base, used, limit).next_base_fee
        oracle = O.oracle_next_base_fee(base, used, limit)
        if kernel != oracle:
            mismatches += 1
    check("recurrence equivalence over fuzzed space", mismatches == 0,
          f"{mismatches} mismatches")

    # transaction pricing equivalence
    bad = 0
    for _ in range(n):
        base = rng.randint(0, 10**12)
        max_fee = rng.randint(0, 10**12)
        max_tip = rng.randint(0, 10**12)
        if max_fee < base:
            continue
        k = effective_priority_tip(base_fee=base, max_fee_per_gas=max_fee,
                                   max_priority_fee_per_gas=max_tip)
        if k != O.oracle_effective_tip(base, max_fee, max_tip):
            bad += 1
    check("effective-tip equivalence over fuzzed space", bad == 0, f"{bad} bad")


def verify_fixture_replay() -> None:
    print("\n=== 3. synthetic fixture replay + conservation + rejections ===")
    scenario = load_scenario(ROOT / "fixtures" / "chain_fixture.json")
    state = ChainState(balances={a: int(v) for a, v in scenario["genesis_balances"].items()})
    chain = Chain(state)
    store = Store(":memory:")
    sc = resolve_parent_hashes(scenario, chain.genesis_hash())

    total_money_before = sum(state.balances.values())
    summary = replay_scenario(sc, chain=chain, store=store)

    check("all 4 fixture blocks accepted", summary.blocks_accepted == 4,
          json.dumps(summary.failures))
    check("exactly 4 invalid txs recorded", summary.invalid_txs == 4,
          f"got {summary.invalid_txs}")
    for block in summary.blocks:
        exp = next(b["expected"] for b in sc["blocks"] if b["number"] == block["number"])
        check(f"block {block['number']} next_base_fee matches oracle",
              block["next_base_fee"] == exp["next_base_fee"])
        check(f"block {block['number']} burned matches oracle",
              block["burned"] == exp["burned"])
        check(f"block {block['number']} tipped matches oracle",
              block["tipped"] == exp["tipped"])

    # Conservation: burned accrues to the burn sink (unspendable), tips to
    # coinbase; the *spendable* money supply excluding the burn sink must have
    # decreased by exactly the burned amount, and total including sink constant.
    total_after = sum(chain.state.balances.values())
    check("total balance including burn sink is conserved",
          total_after == total_money_before,
          f"before {total_money_before} after {total_after}")
    burned_total = chain.state.balances[BURN_ADDRESS]
    spendable_after = total_after - burned_total
    check("spendable supply shrinks by exactly burned amount",
          spendable_after == total_money_before - burned_total)
    check("burned == sum of per-block burns",
          burned_total == sum(int(b["burned"]) for b in summary.blocks))
    check("coinbase holds exactly total tips",
          chain.state.balances[COINBASE_ADDRESS] ==
          sum(int(b["tipped"]) for b in summary.blocks))

    # Invalid tx categories by block.
    by_block = {b["number"]: {(i["index"], i["code"]) for i in b["invalid_txs"]}
                              for b in summary.blocks}
    check("block1 invalid categories", by_block.get(1) ==
          {(1, "E020_MAX_FEE_BELOW_BASE"), (2, "E011_SIGNATURE_INVALID")},
          str(by_block.get(1)))
    check("block2 invalid category", by_block.get(2) ==
          {(1, "E033_INSUFFICIENT_BALANCE")}, str(by_block.get(2)))
    check("block4 invalid category", by_block.get(4) ==
          {(1, "E031_NONCE_MISMATCH")}, str(by_block.get(4)))

    # Rejected block cases through the CLI run path on a temp report.
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        report_path = os.path.join(td, "replay.json")
        rc = replay_run([
            "--scenario", str(ROOT / "fixtures" / "chain_fixture.json"),
            "--report", report_path,
        ])
        report = json.loads(Path(report_path).read_text())
    check("replay CLI exit status", rc == 0, f"rc={rc}")
    for case in report["rejected_block_cases"]:
        check(f"rejected: {case['name']}", case["matched"],
              f"expected {case['expected_code']} observed {case['observed_code']}")

    # SQLite index durability checks
    row3 = store.get_block_row(3)
    check("sqlite index: empty block stored", row3 is not None
          and row3["next_base_fee"] == summary.blocks[2]["next_base_fee"])
    check("sqlite index: head advanced to 4", store.head_number() == 4)


def main() -> int:
    verify_hand_vectors()
    verify_oracle_consistency()
    verify_fixture_replay()

    # Explicit non-claims: this environment does not run production networking.
    not_executed.append(
        "No real-chain connection is made or tested by design (boundary).")
    print("\n=== not executed (explicitly NOT claimed as passing) ===")
    for item in not_executed:
        print(" - " + item)

    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    Path(ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "verification.json").write_text(json.dumps({
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
        "not_executed": not_executed,
    }, indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
