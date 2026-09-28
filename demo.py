#!/usr/bin/env python3
"""Local end-to-end demo of the header light client.

No network, no accounts, no real chain data. It builds a synthetic test
chain with the independent fixture builder, boots a kernel backed by a
temporary SQLite file, and walks through:

  1. bootstrap from a signed trusted checkpoint
  2. continuous legitimate headers (incl. a committee rotation)
  3. insufficient-weight header        -> rejected, state unchanged
  4. conflicting/equivocating header   -> rejected, state unchanged
  5. previous-era committee signing    -> rejected, state unchanged
  6. untrusted branch (unknown parent) -> rejected, state unchanged
  7. trust-period boundary: exact edge accepted, +1s -> NEED_CHECKPOINT

Each step prints run id, key intermediate state, the concrete result and
the failure category. Exit code is 0 if every expectation holds.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lightclient import codec
from lightclient.config import LightClientConfig
from lightclient.errors import ErrorCode, LightClientError
from lightclient.fixtures.builder import (
    ChainBuilder,
    build_certificate,
    build_header,
)
from lightclient.kernel import LightClientKernel
from lightclient.store import Store

RUN_ID = "demo-local-001"


def _digest(h):
    return codec.header_digest(h).hex()[:12]


def _state(k):
    t = k.tip()
    return f"tip(h={t.height}, r={t.round}, ep={t.epoch}, ts={t.timestamp}, {_digest_digest(t.digest)})"


def _digest_digest(b):
    return b.hex()[:12]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lc-demo-"))
    db = tmp / "demo.db"
    print(f"[run_id={RUN_ID}] sqlite: {db}")
    print("NOTE: simplified LOCAL test-chain protocol, not a public-chain client\n")

    builder = ChainBuilder()
    genesis, envelope = builder.genesis(timestamp=1_000_000)
    cfg = LightClientConfig()
    kernel = LightClientKernel(
        Store(str(db)), cfg, builder.checkpoint_pub, run_id=RUN_ID
    )

    failures = []

    def check(label, ok, detail=""):
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {label} {detail}")
        if not ok:
            failures.append(label)

    # 1. bootstrap
    tip = kernel.bootstrap(envelope)
    check("1. bootstrap signed checkpoint", tip.height == 0, _state(kernel))

    # second bootstrap must fail (construct a distinct checkpoint without
    # disturbing the builder's chain cursor)
    try:
        from lightclient.types import Checkpoint, CheckpointEnvelope
        from lightclient.crypto import sign_checkpoint

        cp2 = Checkpoint(
            chain_id=builder.chain_id,
            header=genesis,
            committee=builder.genesis_secrets.committee,
            trust_period_seconds=cfg.trust_period_seconds,
        )
        env2 = CheckpointEnvelope(
            checkpoint=cp2, signature=sign_checkpoint(builder.checkpoint_seed, cp2)
        )
        kernel.bootstrap(env2)
        check("1b. re-bootstrap refused", False)
    except LightClientError as e:
        check(
            "1b. re-bootstrap refused",
            e.code == ErrorCode.ALREADY_INITIALIZED,
            f"-> {e.code.value}",
        )

    # 2. legitimate chain with rotation at height 3
    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])
    b1 = builder.add_block(signer_labels=["c0-a", "c0-b"])
    b2 = builder.add_block(signer_labels=["c0-b", "c0-c"])
    b3 = builder.add_block(signer_labels=["c0-a", "c0-c"], next_committee=c1.committee)
    b4 = builder.add_block(signer_labels=["c1-a", "c1-b"], epoch=1)
    for label, blk in [("2a h1", b1), ("2b h2", b2), ("2c h3 announce c1", b3),
                       ("2d h4 signed by c1", b4)]:
        r = kernel.apply_header(blk.header, blk.certificate)
        check(
            f"2. {label} accepted",
            r.decision == "accepted",
            f"weight={r.certificate.signed_weight} {_state(kernel)}",
        )

    snapshot = (kernel.tip().digest, kernel.tip().height)

    # helper for rejection expectations
    def expect_reject(label, header, cert, expected_code):
        try:
            kernel.apply_header(header, cert)
            check(label, False, "was ACCEPTED unexpectedly")
        except LightClientError as e:
            unchanged = (kernel.tip().digest, kernel.tip().height) == snapshot
            check(
                label,
                e.code == expected_code and unchanged,
                f"-> {e.code.value}/{e.category.value}, state_unchanged={unchanged}",
            )

    # 3. weight insufficient
    low = builder.add_block(signer_labels=["c1-a"], epoch=1,
                            height=5, round=5, timestamp=b4.header.timestamp + 10,
                            parent_digest=snapshot[0])
    expect_reject("3. weight 1 < quorum 2", low.header, low.certificate,
                  ErrorCode.WEIGHT_BELOW_QUORUM)

    # 4. conflicting header: another validly-signed block at height 4
    conflict = build_header(
        chain_id=builder.chain_id, height=4, round=99, epoch=1,
        timestamp=b3.header.timestamp + 5,
        parent_digest=codec.header_digest(b3.header),
        payload=b"\xee" * 32,
    )
    conflict_cert = build_certificate(
        conflict,
        [builder.committees[1].seed_for("c1-a"),
         builder.committees[1].seed_for("c1-b")],
    )
    expect_reject("4. equivocation at h4", conflict, conflict_cert,
                  ErrorCode.CONFLICTING_HEADER)

    # 5. previous-era committee signs an epoch-1 header
    stale = builder.add_block(
        signer_labels=["c0-a", "c0-b"], epoch=1,
        committee_for_signing=builder.committees[0],
        height=5, round=6, timestamp=b4.header.timestamp + 20,
        parent_digest=snapshot[0],
    )
    expect_reject("5. old committee signs new era", stale.header, stale.certificate,
                  ErrorCode.STALE_COMMITTEE)

    # 6. untrusted branch (unknown parent)
    orphan = builder.add_block(
        signer_labels=["c1-a", "c1-b"], epoch=1,
        parent_digest=b"\x42" * 32,
        height=5, round=7, timestamp=b4.header.timestamp + 30,
    )
    expect_reject("6. branch off unknown parent", orphan.header, orphan.certificate,
                  ErrorCode.PARENT_UNKNOWN)

    # 7. trust-period boundary off current tip.
    # Prove the +1-second case is refused FIRST (state unchanged), then the
    # exact-boundary header (same height/parent) is accepted.
    tip_ts = kernel.tip().timestamp
    over = builder.add_block(
        signer_labels=["c1-b", "c1-c"], epoch=1,
        timestamp=tip_ts + cfg.trust_period_seconds + 1,
        height=5, round=8, parent_digest=snapshot[0],
    )
    try:
        kernel.apply_header(over.header, over.certificate)
        check("7a. header 1s past trust period -> NEED_CHECKPOINT", False)
    except LightClientError as e:
        unchanged = (kernel.tip().digest, kernel.tip().height) == snapshot
        check("7a. header 1s past trust period -> NEED_CHECKPOINT",
              e.code == ErrorCode.NEED_CHECKPOINT and unchanged,
              f"-> {e.code.value} gap={e.detail.get('gap_seconds')}s, "
              f"state_unchanged={unchanged}")

    at_edge = builder.add_block(
        signer_labels=["c1-b", "c1-c"], epoch=1,
        timestamp=tip_ts + cfg.trust_period_seconds,
        height=5, round=9, parent_digest=snapshot[0],
    )
    r = kernel.apply_header(at_edge.header, at_edge.certificate)
    check("7b. header exactly at trust boundary accepted",
          r.decision == "accepted", f"gap={cfg.trust_period_seconds}s "
          f"{'(<= trust_period)' }")

    # audit trail summary
    audit = kernel.store.list_audit(100)
    by_result = {}
    for a in audit:
        by_result[a["result"]] = by_result.get(a["result"], 0) + 1
    print(f"\naudit entries: {by_result} (stored in {db})")
    print("inspect with: sqlite3 <db> 'select result, error_code, detail from audit;'")

    kernel.store.close()
    if failures:
        print(f"\nDEMO FAILED: {len(failures)} expectation(s): {failures}")
        return 1
    print("\nDEMO OK: all accept/reject expectations held; rejected inputs "
          "never moved trusted state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
