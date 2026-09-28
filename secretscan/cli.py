"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .errors import SecretscanError
from .idutils import new_request_id
from .logging_setup import configure_logging, get_logger
from .report import build_report, render_markdown
from .rules import load_rules
from .service import ScanService
from .storage import Store
from .version import __version__

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"
DEFAULT_STATE = Path(os.environ.get("SECRETSCAN_STATE", str(Path.cwd() / ".secretscan_state")))


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, sort_keys=True, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _actor() -> str:
    return os.environ.get("SECRETSCAN_ACTOR") or getpass.getuser()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="secretscan", description="Offline secret candidate scanner")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="rules YAML path")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE), help="state directory")
    p.add_argument("--json-logs", action="store_true", help="emit JSON logs to stderr")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scan", help="scan a local snapshot directory")
    sp.add_argument("root")
    sp.add_argument("--note", default="")

    rp = sub.add_parser("report", help="render the latest (or given) scan report")
    rp.add_argument("project_id_or_root")
    rp.add_argument("--scan-id", default=None)
    rp.add_argument("--format", choices=["json", "md"], default="json")

    bl = sub.add_parser("baseline", help="manage content-fingerprint baseline exemptions")
    bl.add_argument("action", choices=["list", "accept", "revoke"])
    bl.add_argument("project_id")
    bl.add_argument("--rule-id")
    bl.add_argument("--fingerprint")
    bl.add_argument("--note", default="")

    sub.add_parser("projects", help="list registered projects")

    au = sub.add_parser("audit", help="show the project audit trail")
    au.add_argument("project_id")
    au.add_argument("--limit", type=int, default=50)

    cand = sub.add_parser("candidates", help="list candidates for a project")
    cand.add_argument("project_id")
    cand.add_argument("--state", default=None)

    sub.add_parser("version")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.json_logs or args.command != "version":
        configure_logging()
    logger = get_logger()

    try:
        if args.command == "version":
            _print_json({"version": __version__})
            return 0

        # Validate config early with a clear failure code.
        ruleset = load_rules(args.config)
        with Store(args.state_dir) as store:
            service = ScanService(store, args.config)
            actor = _actor()
            request_id = new_request_id()

            if args.command == "scan":
                logger.info(
                    "scan start",
                    extra={"request_id": request_id, "actor": actor, "step": "scan.start"},
                )
                result = service.run_scan(
                    args.root, actor=actor, request_id=request_id, note=args.note
                )
                report = build_report(result)
                _print_json({"request_id": request_id, "report": report})
                return 0

            if args.command == "projects":
                _print_json({"projects": store.list_projects()})
                return 0

            if args.command == "report":
                pid = args.project_id_or_root
                if not pid.startswith("proj_"):
                    from .idutils import project_id_for
                    pid = project_id_for(str(Path(pid).resolve()))
                result = (
                    service.get_scan(pid, args.scan_id)
                    if args.scan_id
                    else service.latest_scan(pid)
                )
                report = build_report(result)
                if args.format == "md":
                    sys.stdout.write(render_markdown(report))
                else:
                    _print_json(report)
                return 0

            if args.command == "baseline":
                if args.action == "list":
                    _print_json({"exemptions": service.list_baseline(args.project_id)})
                elif args.action == "accept":
                    if not args.rule_id or not args.fingerprint:
                        raise SecretscanError(
                            "accept requires --rule-id and --fingerprint",
                            code="validation_error",
                        )
                    out = service.accept_baseline(
                        args.project_id,
                        rule_id=args.rule_id,
                        fingerprint=args.fingerprint,
                        actor=actor,
                        request_id=request_id,
                        note=args.note,
                    )
                    _print_json(out)
                else:
                    if not args.rule_id or not args.fingerprint:
                        raise SecretscanError(
                            "revoke requires --rule-id and --fingerprint",
                            code="validation_error",
                        )
                    out = service.revoke_baseline(
                        args.project_id,
                        rule_id=args.rule_id,
                        fingerprint=args.fingerprint,
                        actor=actor,
                        request_id=request_id,
                    )
                    _print_json(out)
                return 0

            if args.command == "audit":
                _print_json({"events": service.audit_trail(args.project_id, limit=args.limit)})
                return 0

            if args.command == "candidates":
                _print_json({
                    "candidates": service.list_candidates(args.project_id, state=args.state)
                })
                return 0

    except SecretscanError as exc:
        logger.error(
            "command failed: %s",
            exc.message,
            extra={"code": exc.code, "request_id": new_request_id(), "step": "error"},
        )
        _print_json({"ok": False, "error": exc.to_dict()})
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
