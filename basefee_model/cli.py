"""Command-line entry point: offline replay + base-fee query.

Examples
--------
Replay a fixture file into a SQLite index store::

    python -m basefee_model.cli replay data/canonical_fixture.json \
        --db run.db --request-id run-001

Print the next base fee for explicit parent parameters::

    python -m basefee_model.cli next --parent-base-fee 1000000000 \
        --parent-gas-used 0 --parent-gas-limit 30000000

Print the stored timeline / totals::

    python -m basefee_model.cli timeline --db run.db
    python -m basefee_model.cli totals   --db run.db

Exit code is non-zero if any block fails validation, so this is safe to use
in CI.
"""

from __future__ import annotations

import argparse
import json
import sys

from .core.fees import base_fee_step_report
from .fixtures import read_fixture
from .replay.events import EventLog
from .replay.replay import Replayer, payload_from_dict
from .storage.store import IndexStore


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _cmd_replay(args) -> int:
    data = read_fixture(args.fixture)
    store = IndexStore(args.db)
    log = EventLog(enabled=not args.quiet)
    replayer = Replayer(
        store,
        genesis_base_fee=data["genesis_base_fee"],
        gas_limit=data["gas_limit"],
        alloc=data.get("alloc", {}),
        genesis_gas_used=data["gas_limit"] // 2,
        chain_id=data.get("chain_id", 1559),
        log=log,
    )
    payloads = [payload_from_dict(b) for b in data["blocks"]]
    report = replayer.run(payloads, request_id=args.request_id)
    _print(report.to_dict())
    store.close()
    return 0 if report.ok() else 2


def _cmd_next(args) -> int:
    report = base_fee_step_report(args.parent_base_fee, args.parent_gas_used,
                                  args.parent_gas_limit)
    _print(report)
    return 0


def _cmd_timeline(args) -> int:
    with IndexStore(args.db) as store:
        _print(store.base_fee_timeline())
    return 0


def _cmd_totals(args) -> int:
    with IndexStore(args.db) as store:
        _print(store.totals())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="basefee_model",
                                description="Offline EIP-1559 base-fee model")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("replay", help="replay a fixture JSON into SQLite")
    pr.add_argument("fixture")
    pr.add_argument("--db", default="basefee_model.db")
    pr.add_argument("--request-id", default=None)
    pr.add_argument("--quiet", action="store_true")
    pr.set_defaults(func=_cmd_replay)

    pn = sub.add_parser("next", help="compute next base fee from parent")
    pn.add_argument("--parent-base-fee", type=int, required=True)
    pn.add_argument("--parent-gas-used", type=int, required=True)
    pn.add_argument("--parent-gas-limit", type=int, required=True)
    pn.set_defaults(func=_cmd_next)

    pt = sub.add_parser("timeline", help="list stored base-fee timeline")
    pt.add_argument("--db", default="basefee_model.db")
    pt.set_defaults(func=_cmd_timeline)

    pto = sub.add_parser("totals", help="stored conservation totals")
    pto.add_argument("--db", default="basefee_model.db")
    pto.set_defaults(func=_cmd_totals)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
