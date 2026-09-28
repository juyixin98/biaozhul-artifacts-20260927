"""Command-line entry points.

Examples
--------
Replay a fixture offline into a fresh SQLite database::

    python -m reorgindex.replay.cli replay fixtures/short_fork/recording.json \\
        --db run/short_fork.db --report run/reports/short_fork.json

Verify that the stored index equals a full rebuild into a second database::

    python -m reorgindex.replay.cli verify fixtures/short_fork/recording.json \\
        --db run/short_fork.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .rebuild import rebuild_file
from .verify import verify_rebuild_consistency


def _load_meta(recording_path: Path) -> tuple[dict, set, int]:
    recording = json.loads(recording_path.read_text(encoding="utf-8"))
    allowed = {int(b["block"]["difficulty"]) for b in recording["blocks"]}
    return recording, allowed, int(recording["finality_depth"])


def cmd_replay(args: argparse.Namespace) -> int:
    recording_path = Path(args.recording)
    _, allowed, depth = _load_meta(recording_path)
    recording = json.loads(recording_path.read_text(encoding="utf-8"))
    report = rebuild_file(
        recording_path,
        args.db,
        allowed_difficulties=allowed,
        finality_depth=depth,
        authorized_producers={recording["producer_address"]},
    )
    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "fixture": report["fixture"],
        "active_tip": report["active_tip"],
        "active_height": report["active_height"],
        "pending_count": report["pending_count"],
        "decisions": [
            {"name": d["name"], "outcome": d["outcome"], "reason": d.get("reason"),
             "rollback": (d.get("switch") or {}).get("rollback_height_range")
             or (d.get("switch") or {}).get("rollback_heights")}
            for d in report["decisions"]
        ],
        "accounts": report["accounts"],
        "report_written_to": args.report,
    }
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    recording_path = Path(args.recording)
    recording = json.loads(recording_path.read_text(encoding="utf-8"))
    allowed = {int(b["block"]["difficulty"]) for b in recording["blocks"]}
    depth = int(recording["finality_depth"])
    rebuilt_db = str(args.db) + ".rebuilt"
    result = verify_rebuild_consistency(
        recording_path,
        args.db,
        rebuilt_db,
        allowed_difficulties=allowed,
        finality_depth=depth,
        authorized_producers={recording["producer_address"]},
    )
    print(json.dumps({
        "consistent": result["consistent"],
        "live_active_tip": result["live_active_tip"],
        "rebuilt_active_tip": result["rebuilt_active_tip"],
        "event_counts": result["event_counts"],
    }, indent=2))
    return 0 if result["consistent"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reorg-replay")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_replay = sub.add_parser("replay", help="replay a fixture into a fresh DB")
    p_replay.add_argument("recording")
    p_replay.add_argument("--db", required=True)
    p_replay.add_argument("--report")
    p_replay.set_defaults(func=cmd_replay)

    p_verify = sub.add_parser("verify", help="compare live DB with a full rebuild")
    p_verify.add_argument("recording")
    p_verify.add_argument("--db", required=True)
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
