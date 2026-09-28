"""CLI: review a template without starting the HTTP service.

Examples
--------
python -m sqlguard.cli review \\
    --template "SELECT id FROM users WHERE id IN (?)" --params '[1,2,3]'

Output is JSON, carries a request id, and never prints bound values verbatim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .core.policy import load_policy
from .logging_setup import configure_logging
from .state.audit import AuditStore, load_or_create_key
from .state.fixture import fixture_from_dir
from .service import ReviewService


def _build_service(settings: Settings) -> ReviewService:
    logger = configure_logging(settings.log_level)
    policy = load_policy(settings.policy_path)
    fdir = Path(settings.fixture_dir)
    fixture = fixture_from_dir(fdir) if (fdir / "schema.sql").exists() or \
        (fdir / "fixture.db").exists() else None
    key = load_or_create_key(settings.audit_key_path)
    audit = AuditStore(settings.audit_db_path, key)
    return ReviewService(policy, fixture, audit, logger)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sqlguard")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rv = sub.add_parser("review", help="review one template")
    rv.add_argument("--template", required=True)
    rv.add_argument("--params", default="{}",
                    help="JSON object of bindings, e.g. '{\"0\": 1}'")
    rv.add_argument("--slots", default="{}",
                    help="JSON object of identifier slot values")
    rv.add_argument("--request-id", default=None)

    sub.add_parser("verify-chain", help="verify the audit hash chain")

    args = ap.parse_args(argv)
    settings = Settings.load(args.config)
    svc = _build_service(settings)

    if args.cmd == "verify-chain":
        print(json.dumps(svc.chain_report(), indent=2))
        return 0 if svc.chain_report()["ok"] else 2

    try:
        params = json.loads(args.params)
        slots = json.loads(args.slots)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON in --params/--slots: {exc}"}))
        return 2

    response = svc.review(
        template=args.template, params=params, slots=slots,
        request_id=args.request_id)
    out = {"request_id": response.request_id, **response.result}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if response.result["verdict"] == "accept" else 1


if __name__ == "__main__":
    sys.exit(main())
