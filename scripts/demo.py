#!/usr/bin/env python3
"""End-to-end local demo (no network): commit -> disclose -> verify.

Writes request/response samples into docs/samples/ so the documented examples
are actual program output rather than hand-written approximations.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.audit.logging_config import bind_run, configure_logging  # noqa: E402
from app.config import Settings  # noqa: E402
from app.parsing.fixtures import fixture_payload  # noqa: E402
from app.security.saltpolicy import SaltPolicy  # noqa: E402
from app.service import CommitmentService  # noqa: E402
from app.storage import Database  # noqa: E402
from independent.verifier import independent_verify  # noqa: E402

SAMPLES = ROOT / "docs" / "samples"


def main() -> int:
    shutil.rmtree(ROOT / "data", ignore_errors=True)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    settings = Settings()  # type: ignore[call-arg]
    logger = configure_logging(settings)
    db = Database(settings.db_path)
    policy = SaltPolicy(
        digest_name=settings.digest.value,
        salt_bytes=settings.default_salt_bytes,
    )
    log = bind_run(logger, "run-demo-0001")
    svc = CommitmentService(db, policy, log)

    payload = fixture_payload()
    (SAMPLES / "01-create-batch.request.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False)
    )
    created = svc.create_batch(
        batch_id=payload["batch_id"],
        fields_raw=payload["fields"],
        records_raw=payload["records"],
    )
    (SAMPLES / "02-create-batch.response.json").write_text(
        json.dumps({"run_id": log.run_id, **created}, indent=2, ensure_ascii=False)
    )
    root = created["batch_root_hex"]

    disclose_req = {
        "batch_id": payload["batch_id"],
        "record_index": 0,
        "path": "subject.age",
    }
    (SAMPLES / "03-disclose.request.json").write_text(json.dumps(disclose_req, indent=2))
    proof = svc.disclose(**disclose_req)
    (SAMPLES / "04-disclose.response.json").write_text(
        json.dumps({"run_id": log.run_id, "proof": proof}, indent=2, ensure_ascii=False)
    )

    verify_req = {
        "proof": proof,
        "trusted_batch_root_hex": root,
        "expected_path": "subject.age",
        "expected_record_index": 0,
    }
    (SAMPLES / "05-verify.request.json").write_text(
        json.dumps(verify_req, indent=2, ensure_ascii=False)
    )
    verdict = svc.verify(
        proof=proof,
        trusted_root_hex=root,
        expected_path="subject.age",
        expected_record_index=0,
    )
    (SAMPLES / "06-verify.response.json").write_text(
        json.dumps({"run_id": log.run_id, **verdict}, indent=2, ensure_ascii=False)
    )

    ok, cat, reason, _ = independent_verify(
        proof, root, expected_path="subject.age", expected_record=0
    )
    print("batch_root:", root)
    print("service verify: valid=", verdict["valid"], "category=", verdict["category"])
    print("independent verify: valid=", ok, "category=", cat, "reason=", reason)
    print("samples written to", SAMPLES)
    return 0 if verdict["valid"] and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
