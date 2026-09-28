"""Offline replay CLI.

Rebuilds state from an exported journal file and reports accept/reject with
the record seq and key state. Exits non-zero on the first failure.

Example:
    python -m smt.tools.replay --journal data/sample/journal.json
"""
from __future__ import annotations

import argparse
import json
import sys

from ..config import get_settings
from ..kernel.store import MemoryNodeStore
from ..services.replay import ReplayError, load_journal_file, replay_records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay and verify a signed journal")
    parser.add_argument("--journal", required=True, help="path to journal JSON file")
    parser.add_argument(
        "--hmac-key",
        default=None,
        help="journal HMAC key (defaults to SMT_JOURNAL_HMAC_KEY / dev key)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    hmac_key = args.hmac_key if args.hmac_key is not None else settings.journal_hmac_key

    try:
        records = load_journal_file(args.journal)
    except FileNotFoundError:
        print(json.dumps({"result": "rejected", "category": "missing_file",
                          "detail": args.journal}))
        return 2
    except ReplayError as exc:
        print(json.dumps({"result": "rejected", "category": exc.category, "detail": str(exc)}))
        return 2

    store = MemoryNodeStore()
    try:
        outcome = replay_records(records, store, hmac_key)
    except ReplayError as exc:
        print(json.dumps({
            "result": "rejected",
            "category": exc.category,
            "seq": exc.seq,
            "detail": str(exc),
            "state": exc.state,
        }, ensure_ascii=False, indent=2))
        return 1

    report = {
        "result": "accepted",
        "applied": outcome.applied,
        "skipped_noop": outcome.skipped_noop,
        "seq_range": [outcome.first_seq, outcome.last_seq],
        "final_root": outcome.final_root.hex(),
        "signed_final_root": outcome.expected_final_root.hex(),
        "root_matches": outcome.root_matches,
        "nodes_rebuilt": len(store),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not outcome.root_matches:
        return 1
    if not args.quiet:
        print("OK: journal replay verified", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
