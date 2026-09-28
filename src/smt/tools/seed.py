"""Seed a fresh database with the synthetic fixture and export artifacts.

Outputs:
    <out>/journal.json     signed append-only journal (for offline replay)
    <out>/manifest.json    roots after each stage + sample proofs
    <out>/README.txt       short pointer to docs/runbook.md

Usage:
    python -m smt.tools.seed --db data/runtime/seed.db --out data/sample
"""
from __future__ import annotations

import argparse
import json
import os

from ..config import Settings
from ..crypto.hashing import empty_at
from ..services.state_service import StateService
from ..storage.node_store import SqliteNodeStore
from .sample_data import ABSENT_ELSEWHERE, ABSENT_IN_SUBTREE, FIXTURE


def _settings(db_path: str, hmac_key: str) -> Settings:
    return Settings(sqlite_path=db_path, journal_hmac_key=hmac_key)


def build(db_path: str, out_dir: str, hmac_key: str = "dev-only-journal-key-change-me") -> dict:
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    store = SqliteNodeStore(db_path)
    svc = StateService(store, hmac_key=hmac_key)

    empty_root = empty_at(0).hex()
    stage_roots = [{"stage": "empty", "revision": svc.revision, "root": empty_root}]

    # Deterministic batch: FIXTURE is already sorted by key.
    svc.apply_batch([(k, v) for k, v in FIXTURE])
    stage_roots.append({"stage": "after_batch", "revision": svc.revision, "root": svc.root.hex()})
    batch_root = svc.root.hex()

    # A delete + re-insert of k_a to exercise the journal's history.
    k_a = FIXTURE[0][0]
    svc.update_one(k_a, None)
    stage_roots.append({"stage": "after_delete_ka", "revision": svc.revision, "root": svc.root.hex()})
    svc.update_one(k_a, FIXTURE[0][1])
    stage_roots.append({"stage": "after_reinsert_ka", "revision": svc.revision, "root": svc.root.hex()})

    journal = store.journal_rows()
    manifest = {
        "spec": "smt-v1",
        "hmac_note": "signed with the development key; rotate via SMT_JOURNAL_HMAC_KEY",
        "inserted": [{"key": k, "value": v} for k, v in FIXTURE],
        "absent_keys": [ABSENT_IN_SUBTREE, ABSENT_ELSEWHERE],
        "stages": stage_roots,
        "final_root": svc.root.hex(),
        "batch_root": batch_root,
        "final_equals_batch_root": svc.root.hex() == batch_root,
        "sample_proofs": {
            "membership_ka": svc.issue_proof(k_a),
            "nonmembership_in_subtree": svc.issue_proof(ABSENT_IN_SUBTREE),
            "nonmembership_elsewhere": svc.issue_proof(ABSENT_ELSEWHERE),
        },
    }

    journal_path = os.path.join(out_dir, "journal.json")
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(journal_path, "w", encoding="utf-8") as fh:
        json.dump({"version": "smt-v1", "records": journal}, fh, indent=2, ensure_ascii=False)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    store.close()
    return {"journal": journal_path, "manifest": manifest_path, "db": db_path,
            "final_root": manifest["final_root"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic fixture and export journal")
    parser.add_argument("--db", default="data/runtime/seed.db")
    parser.add_argument("--out", default="data/sample")
    parser.add_argument("--hmac-key", default="dev-only-journal-key-change-me")
    args = parser.parse_args(argv)
    result = build(args.db, args.out, args.hmac_key)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
