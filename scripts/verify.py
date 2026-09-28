#!/usr/bin/env python3
"""Standalone verification script for the base-fee recurrence model.

This is deliberately separate from ``pytest``: it is a human-readable,
end-to-end verification report that can be handed to a reviewer and run in an
air-gapped environment. It performs four sections and exits non-zero if any
check fails:

  1. Hand vectors vs the independent reference oracle vs the system under test
     (single-step recurrence, multi-block recurrence, effective prices).
  2. Canonical multi-block replay: exact base-fee timeline + fee conservation.
  3. Boundary vectors: empty / full / target / extremely low base fee,
     minimum increment and floor/truncation.
  4. Negative cases: uint256 overflow and invalid fee caps -> exact categories.

The reference oracle (tests/oracle/reference_oracle.py) does NOT import the
production core, so agreement is a genuine cross-implementation check.

Usage:
    python scripts/verify.py
    python scripts/verify.py --vectors tests/vectors/hand_vectors.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "oracle"))

from basefee_model.core.fees import next_base_fee  # noqa: E402
from basefee_model.core.validation import validate_fee_caps  # noqa: E402
from basefee_model.encoding.transaction import Transaction  # noqa: E402
from basefee_model.errors import TransactionError  # noqa: E402
from basefee_model.fixtures import canonical_scenario  # noqa: E402
from basefee_model.replay.replay import Replayer, payload_from_dict  # noqa: E402
from basefee_model.replay.events import EventLog  # noqa: E402
from basefee_model.storage.store import IndexStore  # noqa: E402
from reference_oracle import (oracle_next_base_fee,  # noqa: E402
                              oracle_effective_price)


class Reporter:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []
        self.unexecuted = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            self.failures.append((name, detail))
            print(f"  FAIL  {name}  {detail}")

    def section(self, title: str) -> None:
        print(f"\n=== {title} ===")

    def cannot_run(self, name: str, reason: str) -> None:
        self.unexecuted.append((name, reason))
        print(f"  N/A   {name}  ({reason})")


def section_vectors(rep: Reporter, vectors: dict) -> None:
    rep.section("1. Hand vectors x independent oracle x system under test")
    for case in vectors["single_step"]:
        sut = next_base_fee(case["parent_base_fee"], case["parent_gas_used"],
                            case["gas_limit"])
        ora = oracle_next_base_fee(case["parent_base_fee"],
                                   case["parent_gas_used"],
                                   case["gas_limit"])
        hand = case["expected_next"]
        rep.check(
            f"recurrence[{case['name']}] = {hand}",
            sut == hand == ora,
            f"system={sut} oracle={ora} hand={hand}",
        )

    spec = vectors["multi_block_recurrence"]
    base, gl = spec["genesis_base_fee"], spec["gas_limit"]
    prev = gl // 2
    ok = True
    for blk in spec["blocks"]:
        sut = next_base_fee(base, prev, gl)
        ora = oracle_next_base_fee(base, prev, gl)
        if not (sut == ora == blk["base_fee"]):
            ok = False
        base, prev = sut, blk["gas_used"]
    rep.check("multi-block recurrence (7 blocks) matches hand timeline", ok)
    rep.check("head base fee after block 7",
              base == spec["next_base_fee_after_block_7"],
              f"got {base} want {spec['next_base_fee_after_block_7']}")

    for case in vectors["effective_gas_price"]:
        if case["tx_type"] == 2:
            tx = Transaction(type=2, nonce=0, gas_limit=21_000, to=b"\x00" * 20,
                             max_fee_per_gas=case["max_fee"],
                             max_priority_fee_per_gas=case["priority"])
            eff, tip = tx.effective_gas_price(case["base_fee"]), \
                tx.priority_fee_per_gas(case["base_fee"])
        else:
            tx = Transaction(type=0, nonce=0, gas_limit=21_000, to=b"\x00" * 20,
                             gas_price=case["gas_price"])
            eff, tip = tx.effective_gas_price(case["base_fee"]), \
                tx.priority_fee_per_gas(case["base_fee"])
        o_eff, o_tip = oracle_effective_price(
            case["tx_type"], case["base_fee"], max_fee=case["max_fee"] or 0,
            priority=case["priority"] or 0, gas_price=case["gas_price"] or 0)
        rep.check(f"effective-price[{case['name']}]",
                  eff == case["effective"] == o_eff
                  and tip == case["tip"] == o_tip,
                  f"eff={eff}/{o_eff} tip={tip}/{o_tip}")


def section_canonical(rep: Reporter) -> None:
    rep.section("2. Canonical multi-block replay: timeline + conservation")
    sc = canonical_scenario()
    db = tempfile.mktemp(suffix=".db")
    store = IndexStore(db)
    replayer = Replayer(store, sc["genesis_base_fee"], sc["gas_limit"],
                        sc["alloc"], log=EventLog(enabled=False), chain_id=1559)
    report = replayer.run([payload_from_dict(b) for b in sc["blocks"]],
                          request_id="verify-canonical")
    rep.check("all 7 blocks applied without failure", report.ok(),
              str(report.failures))
    timeline = store.base_fee_timeline()
    expected_b1 = sc["genesis_base_fee"]
    rep.check("block 1 base fee equals genesis (balanced genesis)",
              timeline[0]["base_fee"] == expected_b1,
              f"got {timeline[0]['base_fee']}")
    # Re-derive expected timeline independently and compare each block.
    base = sc["genesis_base_fee"]
    prev_used = sc["gas_limit"] // 2
    ok = True
    for row, blk in zip(timeline, sc["blocks"]):
        exp = oracle_next_base_fee(base, prev_used, sc["gas_limit"])
        if row["base_fee"] != exp:
            ok = False
        base, prev_used = exp, blk["tx_gas_used"][0] if blk["tx_gas_used"] else 0
    rep.check("stored timeline matches oracle-derived timeline", ok)
    cons = report.conservation
    rep.check("fee conservation (debits == burned + tips + transferred)",
              cons["conserved"], f"difference={cons['difference']}")
    totals = store.totals()
    rep.check("persisted totals conserved", totals["conserved"],
              f"difference={totals['difference']}")
    # Idempotency.
    second = replayer.run([payload_from_dict(b) for b in sc["blocks"]],
                          request_id="verify-canonical-rerun")
    rep.check("replay is idempotent (second run applies nothing)",
              second.applied == [] and second.skipped == list(range(1, 8)),
              f"applied={second.applied}")
    store.close()
    os.remove(db)


def section_boundaries(rep: Reporter) -> None:
    rep.section("3. Boundary vectors: empty/full/target + very low base fee")
    GL = 30_000_000
    T = 15_000_000
    checks = [
        ("target keeps 1e9", next_base_fee(1_000_000_000, T, GL), 1_000_000_000),
        ("empty drops 1e9 by 1/8", next_base_fee(1_000_000_000, 0, GL),
         875_000_000),
        ("full raises 1e9 by 1/8", next_base_fee(1_000_000_000, GL, GL),
         1_125_000_000),
        ("base 1 empty floored", next_base_fee(1, 0, GL), 1),
        ("base 1 full minimum +1", next_base_fee(1, GL, GL), 2),
        ("base 3 near-target down truncation stays",
         next_base_fee(3, T - 1, GL), 3),
        ("base 0 full rises to 1", next_base_fee(0, GL, GL), 1),
        ("base 8 empty -> 7", next_base_fee(8, 0, GL), 7),
        ("base 16 full -> 18", next_base_fee(16, GL, GL), 18),
    ]
    for name, got, want in checks:
        rep.check(name, got == want, f"got {got} want {want}")
    # Very large uint values do not overflow Python (defensive vs EVM bounds).
    big = 2 ** 200
    got = next_base_fee(big, 0, GL)
    rep.check("2^200 base fee empty scales by 1/8 exactly",
              got == big - big // 8, f"got {got}")


def section_negative(rep: Reporter) -> None:
    rep.section("4. Negative cases: overflow & invalid fee caps (categories)")
    MAX = 2 ** 256 - 1

    def expect_code(name, tx, base_fee, want):
        try:
            validate_fee_caps(tx, base_fee)
        except TransactionError as exc:
            rep.check(name, exc.code.value == want,
                      f"got {exc.code.value} want {want}")
            return
        rep.check(name, False, "no error raised")

    expect_code("fee cap overflows uint256",
                Transaction(type=2, nonce=0, gas_limit=1, to=b"\x00" * 20,
                            max_fee_per_gas=MAX + 1,
                            max_priority_fee_per_gas=1), 0,
                "fee_cap_overflows_u256")
    expect_code("upfront cap*gas overflows uint256",
                Transaction(type=2, nonce=0, gas_limit=2, to=b"\x00" * 20,
                            max_fee_per_gas=MAX,
                            max_priority_fee_per_gas=1), 0,
                "fee_cap_overflows_u256")
    expect_code("cap < priority",
                Transaction(type=2, nonce=0, gas_limit=1, to=b"\x00" * 20,
                            max_fee_per_gas=50,
                            max_priority_fee_per_gas=80), 100,
                "fee_cap_less_than_priority")
    expect_code("cap < base fee",
                Transaction(type=2, nonce=0, gas_limit=1, to=b"\x00" * 20,
                            max_fee_per_gas=90,
                            max_priority_fee_per_gas=1), 100,
                "fee_cap_below_base_fee")
    expect_code("legacy gas_price < base fee",
                Transaction(type=0, nonce=0, gas_limit=1, to=b"\x00" * 20,
                            gas_price=90), 100, "fee_cap_below_base_fee")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors",
                        default=os.path.join(ROOT, "tests", "vectors",
                                             "hand_vectors.json"))
    args = parser.parse_args()

    print("EIP-1559 base-fee model -- verification")
    print(f"root: {ROOT}")
    with open(args.vectors, encoding="utf-8") as fh:
        vectors = json.load(fh)
    print("vectors provenance:", vectors["provenance"]["method"][:80] + "...")

    rep = Reporter()
    try:
        section_vectors(rep, vectors)
        section_canonical(rep)
        section_boundaries(rep)
        section_negative(rep)
    except Exception:  # noqa: BLE001 - report unexpected crash as failure
        rep.failed += 1
        rep.failures.append(("verification-crash", traceback.format_exc()))
        print("\nERROR: verification crashed:\n" + traceback.format_exc())

    print("\n=== Summary ===")
    print(f"passed: {rep.passed}")
    print(f"failed: {rep.failed}")
    if rep.unexecuted:
        print("\nChecks not executed (listed separately, NOT counted as pass):")
        for name, reason in rep.unexecuted:
            print(f"  - {name}: {reason}")
    if rep.failed:
        print("\nFailing checks:")
        for name, detail in rep.failures:
            print(f"  - {name}: {detail[:200]}")
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
