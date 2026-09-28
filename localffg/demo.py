"""In-process end-to-end demonstration (no HTTP required).

Each narrative scenario runs on an ISOLATED kernel backed by its own SQLite
database (scenarios deliberately reuse synthetic validator identities), so
the printed categories match the fixture semantics exactly. Afterwards a
final aggregate journal is replayed offline and every emitted evidence
bundle is independently re-checked.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import AppConfig
from .epochs import ValidatorRegistry
from .fixtures_builder import build_fixture_set
from .logging_utils import JsonRunLogger, new_run_id
from .models import SignedVote
from .replay import replay_store
from .service import VoteService


def run_demo(db_path: str, *, cfg: AppConfig | None = None, fixture_dir: str | None = None) -> int:
    cfg = cfg or AppConfig()
    cfg = AppConfig(
        chain_id=cfg.chain_id,
        epoch_length=cfg.epoch_length,
        db_path=db_path,
        domain=cfg.domain,
        http_host=cfg.http_host,
        http_port=cfg.http_port,
        allow_bootstrap_api=cfg.allow_bootstrap_api,
        log_level=cfg.log_level,
    )

    if fixture_dir is None:
        import tempfile

        fixture_dir = tempfile.mkdtemp(prefix="localffg-fixtures-")
    paths = build_fixture_set(fixture_dir)
    manifest = json.loads(Path(paths["manifest"]).read_text(encoding="utf-8"))
    registry_json = manifest["registry"]

    # fresh output directory: one db per scenario + one aggregate db
    out_dir = Path(db_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("demo-*.db*"):
        old.unlink()
    agg_db = out_dir / "demo-aggregate.db"
    if agg_db.exists():
        agg_db.unlink()

    summary: dict[str, list[dict]] = {}
    scenario_services: list[VoteService] = []

    print("\n================ SCENARIO RESULTS (isolated kernel each) ================")
    for scenario, steps in manifest["scenarios"].items():
        svc_cfg = AppConfig(
            chain_id=cfg.chain_id, epoch_length=cfg.epoch_length,
            db_path=str(out_dir / f"demo-{scenario}.db"), domain=cfg.domain,
        )
        logger = JsonRunLogger(run_id=new_run_id(scenario.lower()), echo=False)
        svc = VoteService(svc_cfg, logger=logger)
        svc.install_registry(ValidatorRegistry.from_json(registry_json))
        scenario_services.append(svc)

        rows = []
        for i, step in enumerate(steps, start=1):
            signed = SignedVote.from_json_dict(step["signed"])
            outcome = svc.submit(signed)
            rows.append(
                {
                    "label": step["label"],
                    "step": i,
                    "status": outcome.status.value,
                    "slashable": outcome.slashable,
                    "evidence_ids": [e["evidence_id"] for e in outcome.evidence],
                    "reason": outcome.reason,
                }
            )
        summary[scenario] = rows

        print(f"\n[{scenario}]")
        for r in rows:
            print(
                f"  {r['step']}. {r['label']:<34} -> {r['status']:<24} "
                f"slashable={r['slashable']} ev={len(r['evidence_ids'])}"
            )

    # ---- aggregate totals across the per-scenario journals --------------- #
    totals: dict[str, int] = {}
    evidence_total = 0
    for svc in scenario_services:
        for status, n in svc.store.status_counts().items():
            totals[status] = totals.get(status, 0) + n
        evidence_total += svc.store.evidence_count()

    print("\n================ AGGREGATE JOURNAL CATEGORIES ================")
    print(json.dumps(dict(sorted(totals.items())), indent=2))
    print(f"evidence rows (across scenarios): {evidence_total}")

    # ---- offline replay + independent re-check of EVERY scenario --------- #
    print("\n================ OFFLINE REPLAY + INDEPENDENT RE-CHECK ================")
    all_ok = True
    check_rows = []
    for svc, scenario in zip(scenario_services, manifest["scenarios"].keys()):
        report = replay_store(svc.store, logger=JsonRunLogger(run_id=f"replay-{scenario}", echo=False))
        ok = report.verdict == "OK"
        all_ok &= ok
        print(
            f"  {scenario:<40} replay={report.verdict:<4} "
            f"events={report.events_replayed:<2} matched={report.status_matches:<2} "
            f"evidence={len(report.checker_results)}"
        )
        for cr in report.checker_results:
            check_rows.append((scenario, cr))

    print("\n================ EVIDENCE DETAIL ================")
    for scenario, cr in check_rows:
        print(
            f"  [{scenario}] {cr['evidence_id']}: {cr['verdict']:<7} "
            f"kind={cr['derived_kind']} weight={cr['derived_weight']} @epoch {cr['weight_epoch']}"
        )

    # explicit concrete totals the demo guarantees
    expected_totals = {
        "accepted": 14,
        "duplicate_retransmit": 3,
        "double_vote": 3,
        "surround_vote": 2,
        "invalid_signature": 2,
        "invalid_chain": 1,
        "invalid_rounds": 1,
        "invalid_membership": 2,
        "unknown_validator": 1,
    }
    print("\n================ TOTAL ASSERTIONS ================")
    for key, want in expected_totals.items():
        got = totals.get(key, 0)
        mark = "OK" if got == want else f"MISMATCH (got {got})"
        print(f"  {key:<24} expected={want:<3} got={got:<3} {mark}")
        if got != want:
            all_ok = False

    # evidence: S2(1) S3(1) S3b(1) S10(1) S11(1) = 5
    print(f"  evidence total expected=5 got={evidence_total}")
    if evidence_total != 5:
        all_ok = False

    for svc in scenario_services:
        svc.close()

    print("\nDEMO RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 2
