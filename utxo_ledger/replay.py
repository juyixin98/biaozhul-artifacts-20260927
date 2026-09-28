"""Offline replay / verification.

Two replay modes, both exercising the same production code path
(``LedgerNode.submit_raw_block``):

* ``chain``    -- stream every committed block out of a source database in
                  height order into a *fresh* database and assert the rebuilt
                  tip hash matches the source tip. Used to prove the block log
                  alone is sufficient to reconstruct state.
* ``fixtures`` -- replay self-contained JSONL test cases
                  (see ``fixtures/cases.jsonl``). Each case carries setup
                  blocks, the block under test and the expected verdict/code.
                  A fresh on-disk database is used per case so cases cannot
                  contaminate each other; the database file is kept on failure
                  (its path is printed) for manual inspection.

Run IDs and per-block/per-tx decisions land in the run log (see
:mod:`utxo_ledger.runlog`).
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from .errors import LedgerError
from .node import BlockAttemptResult, LedgerNode
from .runlog import RunLogger
from .storage import SqliteStore


@dataclass
class CaseVerdict:
    name: str
    ok: bool
    accepted: bool
    expected_code: str | None
    actual_code: str | None
    actual_category: str | None
    state_unchanged_on_reject: bool | None
    detail: str = ""


@dataclass
class ReplayReport:
    run_id: str
    mode: str
    cases: list[CaseVerdict] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.ok)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if not c.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "passed": self.passed,
            "failed": self.failed,
            "cases": [c.__dict__ for c in self.cases],
        }


# --------------------------------------------------------------------------- #
# Chain replay
# --------------------------------------------------------------------------- #
def replay_chain(
    source_db: str,
    logdir: str,
    keep_target: str | None = None,
    run_id: str | None = None,
) -> ReplayReport:
    """Rebuild chain state from ``source_db``'s block log into a fresh DB."""
    logger = RunLogger(logdir, run_id=run_id)
    logger.run_start("replay_chain", source_db=os.path.abspath(source_db))
    report = ReplayReport(logger.run_id, "chain")

    src = SqliteStore(source_db)
    target_path = keep_target or os.path.join(
        tempfile.mkdtemp(prefix="utxo-replay-"), "rebuilt.db"
    )
    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    if os.path.exists(target_path):
        os.remove(target_path)
    dst = SqliteStore(target_path)
    node = LedgerNode(dst, logger)

    try:
        tip = src.tip_height
        src_tip_hash = src.tip_hash.hex()
        logger.event("chain_source", source_tip_height=tip, source_tip_hash=src_tip_hash)
        for height in range(1, tip + 1):
            raw = src.get_block_raw(height)
            if raw is None:
                raise LedgerError(
                    ErrorCode.STORAGE_FAILURE,
                    f"source missing block at height {height}",
                )
            res = node.submit_raw_block(raw, seq=height)
            if not res.accepted:
                report.cases.append(
                    CaseVerdict(
                        name=f"block#{height}",
                        ok=False,
                        accepted=False,
                        expected_code=None,
                        actual_code=res.error["code"] if res.error else None,
                        actual_category=res.error["category"] if res.error else None,
                        state_unchanged_on_reject=None,
                        detail="previously-committed block rejected during replay",
                    )
                )
                break
            src_info = src.get_block_info(height)
            match = src_info and src_info["hash"] == res.block_hash
            report.cases.append(
                CaseVerdict(
                    name=f"block#{height}",
                    ok=bool(match),
                    accepted=True,
                    expected_code=None,
                    actual_code=None,
                    actual_category=None,
                    state_unchanged_on_reject=None,
                    detail="" if match else "rebuilt hash differs from source",
                )
            )
        rebuilt_tip = dst.tip_hash.hex()
        tip_ok = rebuilt_tip == src_tip_hash and dst.tip_height == tip
        if not any(c.name == "tip" for c in report.cases):
            report.cases.append(
                CaseVerdict(
                    name="tip",
                    ok=tip_ok,
                    accepted=True,
                    expected_code=None,
                    actual_code=None,
                    actual_category=None,
                    state_unchanged_on_reject=None,
                    detail="" if tip_ok else
                    f"rebuilt tip {rebuilt_tip} != source {src_tip_hash}",
                )
            )
    finally:
        summary = {"passed": report.passed, "failed": report.failed}
        summary_path = logger.close(summary)
        src.close()
        dst.close()

    report.target_db = target_path  # type: ignore[attr-defined]
    report.summary_path = summary_path  # type: ignore[attr-defined]
    return report


# --------------------------------------------------------------------------- #
# Fixture-case replay
# --------------------------------------------------------------------------- #
def _expectation_matches(res: BlockAttemptResult, expect: dict) -> tuple[bool, str]:
    want_accepted = bool(expect.get("accepted", True))
    if res.accepted != want_accepted:
        return False, f"accepted={res.accepted}, expected {want_accepted}"
    if not want_accepted:
        want_code = expect.get("code")
        actual_code = res.error["code"] if res.error else None
        if want_code and actual_code != want_code:
            return False, f"code={actual_code}, expected {want_code}"
        want_cat = expect.get("category")
        actual_cat = res.error["category"] if res.error else None
        if want_cat and actual_cat != want_cat:
            return False, f"category={actual_cat}, expected {want_cat}"
        # Atomicity is itself part of the expectation for rejected blocks.
        if not res.state_unchanged:
            return False, "UTXO set changed despite rejection"
    else:
        if expect.get("fee_total") is not None and res.fee_total != expect["fee_total"]:
            return False, (
                f"fee_total={res.fee_total}, expected {expect['fee_total']}"
            )
    return True, ""


def replay_fixture_file(
    fixture_path: str,
    logdir: str,
    run_id: str | None = None,
) -> ReplayReport:
    logger = RunLogger(logdir, run_id=run_id)
    logger.run_start("replay_fixtures", fixture_file=os.path.abspath(fixture_path))
    report = ReplayReport(logger.run_id, "fixtures")

    with open(fixture_path, "r", encoding="utf-8") as fh:
        cases = [json.loads(line) for line in fh if line.strip()]

    artifact_dir = os.path.join(logdir, logger.run_id + "-dbs")
    os.makedirs(artifact_dir, exist_ok=True)

    for seq, case in enumerate(cases, start=1):
        name = case.get("name", f"case#{seq}")
        expect = case.get("expect", {})
        db_path = os.path.join(artifact_dir, f"case-{seq:03d}.db")
        if os.path.exists(db_path):
            os.remove(db_path)
        store = SqliteStore(db_path)
        node = LedgerNode(store, logger)
        setup_ok = True
        setup_detail = ""
        verdict: CaseVerdict | None = None
        try:
            for j, hex_block in enumerate(case.get("setup_blocks", []), start=1):
                r = node.submit_raw_block(bytes.fromhex(hex_block), seq=seq * 1000 + j)
                if not r.accepted:
                    setup_ok = False
                    setup_detail = (
                        f"setup block {j} rejected: "
                        f"{r.error['code'] if r.error else '?'}"
                    )
                    break
            res = node.submit_raw_block(
                bytes.fromhex(case["block_under_test"]), seq=seq * 1000 + 999
            )
            if setup_ok:
                ok, detail = _expectation_matches(res, expect)
            else:
                ok, detail = False, setup_detail
            verdict = CaseVerdict(
                name=name,
                ok=ok,
                accepted=res.accepted,
                expected_code=expect.get("code"),
                actual_code=res.error["code"] if res.error else None,
                actual_category=res.error["category"] if res.error else None,
                state_unchanged_on_reject=(
                    res.state_unchanged if not res.accepted else None
                ),
                detail=detail,
            )
            report.cases.append(verdict)
            logger.event(
                "case_verdict",
                seq=seq,
                case_name=name,
                ok=ok,
                detail=detail,
                db_path=db_path,
            )
        finally:
            store.close()
            # Failing-case databases are retained for manual inspection.
            if verdict is not None and verdict.ok:
                for suffix in ("", "-wal", "-shm"):
                    p = db_path + suffix
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass

    summary_path = logger.close(
        {"passed": report.passed, "failed": report.failed}
    )
    report.summary_path = summary_path  # type: ignore[attr-defined]
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_report(report: ReplayReport) -> int:
    for c in report.cases:
        mark = "PASS" if c.ok else "FAIL"
        line = f"[{mark}] {c.name}"
        if not c.accepted and c.actual_code:
            line += f" -> {c.actual_category}/{c.actual_code}"
        if c.detail:
            line += f" ({c.detail})"
        print(line)
    print(
        f"\nrun_id={report.run_id}  passed={report.passed}  failed={report.failed}"
    )
    print(f"log: {report.summary_path}")
    return 0 if report.failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline UTXO ledger replay")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_chain = sub.add_parser("chain", help="rebuild state from a block database")
    p_chain.add_argument("--db", required=True)
    p_chain.add_argument("--logdir", default="logs")
    p_chain.add_argument("--keep-target", default=None)

    p_fix = sub.add_parser("fixtures", help="replay a JSONL fixture-case file")
    p_fix.add_argument("--file", dest="fixture_file", required=True)
    p_fix.add_argument("--logdir", default="logs")
    p_fix.add_argument("--report", default=None)

    args = parser.parse_args(argv)
    if args.mode == "chain":
        report = replay_chain(args.db, args.logdir, args.keep_target)
        return _print_report(report)

    report = replay_fixture_file(args.fixture_file, args.logdir)
    rc = _print_report(report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
        print(f"report written: {args.report}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
