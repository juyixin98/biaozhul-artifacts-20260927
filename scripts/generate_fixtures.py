"""Generate every synthetic fixture used by the review suite.

Outputs (deterministic apart from freshly generated Ed25519 keys and PoW
nonces; all values are written to disk so review is offline):

* config/authorized_producers.json -- the single authorized producer address
* fixtures/short_fork/recording.json + expected.json
* fixtures/deep_fork/recording.json  + expected.json
* fixtures/interrupt/recording.json  + expected.json (crash_after = "f1")

Re-run with::

    . .venv/bin/activate && python scripts/generate_fixtures.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reorgindex.crypto.keys import public_key_bytes  # noqa: E402
from reorgindex.replay.builder import BranchBuilder, FixtureKeys  # noqa: E402
from reorgindex.replay.fixture_io import write_recording  # noqa: E402

REGULAR = 4
WEIGHTED = 16
K = 3  # finality depth


def write_producer_config(keys: FixtureKeys) -> None:
    cfg = ROOT / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "authorized_producers.json").write_text(
        json.dumps(
            {
                "addresses": [keys.addresses["producer"]],
                "producer_pubkey": public_key_bytes(keys.producer).hex(),
                "note": "Local synthetic fixture producer; no real network.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def gen_short_fork(keys: FixtureKeys) -> None:
    """Longer main chain loses to a shorter but weighted fork."""
    main = BranchBuilder(keys, difficulty=REGULAR)
    main.genesis(name="g0")  # alice 1000, bob 500

    # --- main suffix m1,m2,m3 (all weight 4)
    # m1 holds the duplicate transaction D (alice nonce 1 -> carol 100).
    dup_tx = main.make_transfer(
        sender="alice", recipient="carol", amount=100, nonce=1
    )
    main.child_with_txs(name="m1", transactions=[dup_tx])
    main.child(name="m2", transfers=[{"sender": "bob", "recipient": "alice", "amount": 50}])
    main.child(name="m3", transfers=[{"sender": "alice", "recipient": "bob", "amount": 20}])

    # --- fork f1 (weight 16) from g0, containing the SAME txid D; f2 weight 4
    fork = main.snapshot(tip_name="g0")
    fork.nonces.clear()  # independent nonce state: alice nonce 1 again
    fork.child_with_txs(name="f1", difficulty=WEIGHTED, transactions=[dict(dup_tx)])
    fork.child(
        name="f2",
        transfers=[{"sender": "alice", "recipient": "bob", "amount": 50}],
    )
    # o1 extends f2 and is fed BEFORE f2 -> must suspend then auto-release.
    fork.child(
        name="o1",
        transfers=[{"sender": "carol", "recipient": "bob", "amount": 10}],
    )

    arrival = ["g0", "m1", "m2", "m3", "o1", "f1", "f2"]
    # Weights at arrival:
    #   tip m3 -> active 16. o1 parent f2 unknown -> PENDING.
    #   f1 -> g0+f1 = 4+16 = 20 > 16 -> SWITCH (m1 depth 3 at tip m3, 3 <= K).
    #   f2 extends f1 and releases o1.
    expected = {
        "arrival_outcomes": {
            "g0": "ACCEPT_EXTEND",
            "m1": "ACCEPT_EXTEND",
            "m2": "ACCEPT_EXTEND",
            "m3": "ACCEPT_EXTEND",
            "o1": "PENDING",
            "f1": "ACCEPT_SWITCH",
            "f2": "ACCEPT_EXTEND",
        },
        "winning_tip": "f2",
        "active_chain_names": ["g0", "f1", "f2"],
        "rollback_height_range_on_f1": [1, 3],
        "duplicate_txid": dup_tx["txid"],
        "duplicate_active_block": "f1",
        "duplicate_occurrences": ["m1", "f1"],
        "duplicate_active_count": 1,
        # g0: alice 1000, bob 500; f1 alice->carol 100; f2 alice->bob 50;
        # released o1: carol->bob 10.
        "final_balances": {
            keys.addresses["alice"]: 850,
            keys.addresses["bob"]: 560,
            keys.addresses["carol"]: 90,
        },
        "released_after_switch": ["o1"],
        "final_tip_name": "o1",
    }

    write_recording(
        ROOT / "fixtures" / "short_fork",
        keys=keys,
        main=main,
        branches={"fork": fork},
        arrival_order=arrival,
        expected=expected,
        scenario="short_fork_wins",
        difficulty=REGULAR,
        finality_depth=K,
    )


def gen_deep_fork(keys: FixtureKeys) -> None:
    """Weighted fork that would win, but it detaches a final block -> rejected."""
    main = BranchBuilder(keys, difficulty=REGULAR)
    main.genesis(name="g0")  # alice 1000, bob 500
    main.child(name="m1", transfers=[{"sender": "alice", "recipient": "bob", "amount": 10}])
    main.child(name="m2", transfers=[{"sender": "alice", "recipient": "bob", "amount": 10}])
    main.child(name="m3", transfers=[{"sender": "alice", "recipient": "bob", "amount": 10}])
    main.child(name="m4", transfers=[{"sender": "bob", "recipient": "carol", "amount": 5}])
    main.child(name="m5", transfers=[{"sender": "alice", "recipient": "carol", "amount": 10}])

    # Weighted fork from g0: 4 + 16 + 4 = 24 > 6*4 = 24 -> need strictly more;
    # use two weighted blocks: 4 + 16 + 16 = 36 > 24.
    fork = main.snapshot(tip_name="g0")
    fork.nonces.clear()
    fork.child(
        name="d1",
        difficulty=WEIGHTED,
        transfers=[{"sender": "alice", "recipient": "carol", "amount": 100}],
    )
    fork.child(
        name="d2",
        difficulty=WEIGHTED,
        transfers=[{"sender": "alice", "recipient": "carol", "amount": 100}],
    )

    arrival = ["g0", "m1", "m2", "m3", "m4", "m5", "d1", "d2"]
    # At d1 arrival tip=m5: active 6*4=24 > candidate 4+16=20 -> stored fork.
    # At d2: candidate 4+16+16=36 > 24 and would detach m1 whose depth below
    # the old tip m5 is 5 confirmations > K=3 (final) -> REORG_FINALIZED.
    expected = {
        "arrival_outcomes": {
            "g0": "ACCEPT_EXTEND",
            "m1": "ACCEPT_EXTEND",
            "m2": "ACCEPT_EXTEND",
            "m3": "ACCEPT_EXTEND",
            "m4": "ACCEPT_EXTEND",
            "m5": "ACCEPT_EXTEND",
            "d1": "ACCEPT_FORK",
            "d2": "REJECTED",
        },
        "rejected_block": "d2",
        "reject_reason": "REORG_FINALIZED",
        "would_rollback_height_range": [1, 5],
        "would_confirmations_of_shallowest": 5,
        "finality_depth": K,
        "active_chain_names": ["g0", "m1", "m2", "m3", "m4", "m5"],
        "winning_tip": "m5",
        "final_balances": {
            keys.addresses["alice"]: 1000 - 10 - 10 - 10 - 10,  # m1,m2,m3,m5
            keys.addresses["bob"]: 500 + 30 - 5,                # +30, -5 to carol
            keys.addresses["carol"]: 5 + 10,
        },
        "fork_balances_never_visible": {
            keys.addresses["carol"]: 15,  # active value, never 200/215
        },
    }
    write_recording(
        ROOT / "fixtures" / "deep_fork",
        keys=keys,
        main=main,
        branches={"fork": fork},
        arrival_order=arrival,
        expected=expected,
        scenario="deep_fork_rejected_finalized",
        difficulty=REGULAR,
        finality_depth=K,
    )


def gen_interrupt(keys: FixtureKeys) -> None:
    """Short weighted fork switch with a crash injected after durable detach."""
    main = BranchBuilder(keys, difficulty=REGULAR)
    main.genesis(name="g0")
    main.child(name="m1", transfers=[{"sender": "alice", "recipient": "bob", "amount": 10}])
    main.child(name="m2", transfers=[{"sender": "alice", "recipient": "bob", "amount": 10}])

    fork = main.snapshot(tip_name="g0")
    fork.nonces.clear()
    fork.child(
        name="f1",
        difficulty=WEIGHTED,
        transfers=[{"sender": "alice", "recipient": "carol", "amount": 100}],
    )
    fork.child(
        name="f2",
        transfers=[{"sender": "carol", "recipient": "bob", "amount": 40}],
    )

    arrival = ["g0", "m1", "m2", "f1", "f2"]
    expected = {
        "arrival_outcomes": {
            "g0": "ACCEPT_EXTEND",
            "m1": "ACCEPT_EXTEND",
            "m2": "ACCEPT_EXTEND",
            "f1": "ACCEPT_SWITCH",   # 4+16=20 > 3*4=12, crash injected here
            "f2": "ACCEPT_EXTEND",
        },
        "crash_after": "f1",
        "post_crash_plan_phase": "DETACHED",
        "rollback_height_range": [1, 2],
        "active_after_resume": ["g0", "f1"],
        "final_active_chain": ["g0", "f1", "f2"],
        "final_tip_name": "f2",
        "final_balances": {
            keys.addresses["alice"]: 900,
            keys.addresses["bob"]: 540,
            keys.addresses["carol"]: 60,
        },
    }
    write_recording(
        ROOT / "fixtures" / "interrupt",
        keys=keys,
        main=main,
        branches={"fork": fork},
        arrival_order=arrival,
        expected=expected,
        scenario="switch_interrupted_then_resumed",
        difficulty=REGULAR,
        finality_depth=K,
        crash_after="f1",
    )


def main() -> None:
    keys = FixtureKeys.create()
    write_producer_config(keys)
    gen_short_fork(keys)
    gen_deep_fork(keys)
    gen_interrupt(keys)
    print("fixtures generated under fixtures/ and config/authorized_producers.json")


if __name__ == "__main__":
    main()
