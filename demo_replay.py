#!/usr/bin/env python3
"""Offline replay demo + problem reproduction artifact generator.

Builds a replay stream containing one deliberately bad header (insufficient
weight), runs the offline ReplayEngine against a fresh SQLite-backed kernel,
and writes a reproducible bundle to ./replay-artifacts/<run_id>/:

    stream.json     the exact header/certificate bytes (hex) fed to replay
    report.json     per-item decisions, failure index, tip before/after
    events.jsonl    ordered audit events with intermediate state + reasons

Replay a problem by rerunning:
    .venv/bin/python demo_replay.py            # fresh deterministic run
The fixtures are deterministic, so the same run reproduces byte-for-byte.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lightclient import codec
from lightclient.config import LightClientConfig
from lightclient.fixtures.builder import ChainBuilder
from lightclient.kernel import LightClientKernel
from lightclient.replay import ReplayEngine, ReplayItem
from lightclient.store import Store


def main() -> int:
    run_id = time.strftime("replay-%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(__file__).resolve().parent / "replay-artifacts" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    builder = ChainBuilder()
    _g, envelope = builder.genesis(timestamp=1_000_000)

    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])
    good1 = builder.add_block(signer_labels=["c0-a", "c0-b"])
    good2 = builder.add_block(signer_labels=["c0-b", "c0-c"])
    announce = builder.add_block(
        signer_labels=["c0-a", "c0-c"], next_committee=c1.committee
    )
    rotated = builder.add_block(signer_labels=["c1-a", "c1-b"], epoch=1)
    bad_weight = builder.add_block(signer_labels=["c1-a"], epoch=1)  # weight 1
    after = builder.add_block(signer_labels=["c1-b", "c1-c"], epoch=1)

    blocks = [good1, good2, announce, rotated, bad_weight, after]
    items = [
        ReplayItem(blk.header, blk.certificate, source=f"block-{i+1}")
        for i, blk in enumerate(blocks)
    ]

    stream = {
        "format": "local-header-lightclient-replay/v1",
        "run_id": run_id,
        "checkpoint_envelope": codec.encode_envelope(envelope).hex(),
        "items": [
            {
                "source": it.source,
                "header": codec.encode_header(it.header).hex(),
                "certificate": codec.encode_certificate(it.certificate).hex(),
            }
            for it in items
        ],
    }
    (out_dir / "stream.json").write_text(json.dumps(stream, indent=2, sort_keys=True))

    store = Store(":memory:")
    kernel = LightClientKernel(
        store, LightClientConfig(), builder.checkpoint_pub, run_id=run_id
    )
    kernel.bootstrap(envelope)
    tip_before = kernel.tip().to_dict()

    report = ReplayEngine(kernel).replay(items, run_id=run_id)
    doc = report.to_dict()
    doc["tip_before_run"] = tip_before
    (out_dir / "report.json").write_text(json.dumps(doc, indent=2, sort_keys=True))

    events = store.list_audit(200)
    (out_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in reversed(events)) + "\n"
    )

    print(f"run_id: {run_id}")
    print(f"artifacts: {out_dir}")
    for step in report.steps:
        if step.error_code:
            print(
                f"  item {step.index} ({step.source}) h={step.height}: "
                f"REJECTED {step.error_code}/{step.error_category} :: {step.reason}"
            )
        else:
            print(f"  item {step.index} ({step.source}) h={step.height}: accepted")
    print(f"status={report.status} applied={report.applied}/{report.total} "
          f"failure_index={report.failure_index}")
    print(f"tip height: {tip_before['height']} -> {report.tip_after['height']}")

    ok = (
        report.status == "stopped"
        and report.applied == 4
        and report.failure_index == 4
        and report.steps[4].error_code == "WEIGHT_BELOW_QUORUM"
        and report.tip_after["height"] == 4
    )
    print("REPLAY DEMO OK" if ok else "REPLAY DEMO FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
