#!/usr/bin/env python3
"""Generate golden vectors with DETERMINISTIC salts.

The service normally uses CSPRNG salts, which makes outputs non-reproducible.
For cross-implementation testing we build the exact same batch using the
deterministic salt derivation in :mod:`app.parsing.fixtures`, emit every field
commitment / record root / batch root and a full disclosure proof for one
field, then independently verify that proof with the stdlib-only verifier.

The golden file therefore carries answers produced by BOTH implementations;
tests replay it and require agreement rather than trusting either side.

Run:  python scripts/generate_golden.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.commitment import field_commitment  # noqa: E402
from app.core.encoding import STATE_MISSING, STATE_NULL, STATE_PRESENT  # noqa: E402
from app.core.merkle import (  # noqa: E402
    authentication_path,
    build_levels,
)
from app.parsing import parse_field_specs, parse_records  # noqa: E402
from app.parsing.fixtures import (  # noqa: E402
    FIXTURE_BATCH_ID,
    deterministic_salt,
    fixture_payload,
)
from independent.verifier import independent_verify  # noqa: E402

DIGEST = "sha256"
OUT = Path(__file__).resolve().parents[1] / "tests" / "golden" / "golden.json"


def main() -> int:
    payload = fixture_payload()
    specs = parse_field_specs(payload["fields"])
    records = parse_records(payload["records"], specs)

    all_field_nodes: list[list[bytes]] = []
    secret_material: dict[tuple[int, str], dict[str, str | None]] = {}
    records_json = []

    for rec_index, row in enumerate(records):
        nodes = []
        fields_json = []
        for pos, spec in enumerate(specs):
            cell = row[spec.path]
            state = cell["state"]
            salt = None
            if state == STATE_MISSING:
                value = None
            else:
                value = cell["value"]
                salt = deterministic_salt(
                    FIXTURE_BATCH_ID, rec_index, pos, spec.path
                )
            commit = field_commitment(
                digest_name=DIGEST,
                batch_id=FIXTURE_BATCH_ID,
                record_index=rec_index,
                position=pos,
                path=spec.path,
                field_type=spec.field_type,
                state=state,
                value=value if state == STATE_PRESENT else None,
                salt=salt,
            )
            nodes.append(commit)
            secret_material[(rec_index, spec.path)] = {
                "state": state,
                "salt_hex": salt.hex() if salt else None,
                "value": value if state == STATE_PRESENT else None,
            }
            fields_json.append(
                {
                    "position": pos,
                    "path": spec.path,
                    "field_type": spec.field_type,
                    "state": state,
                    "commitment_hex": commit.hex(),
                }
            )
        all_field_nodes.append(nodes)
        record_root, field_levels = build_levels("field", DIGEST, nodes)
        records_json.append(
            {
                "record_index": rec_index,
                "record_root_hex": record_root.hex(),
                "fields": fields_json,
                "_field_levels": field_levels,
            }
        )

    record_nodes = [bytes.fromhex(r["record_root_hex"]) for r in records_json]
    batch_root, record_levels = build_levels("record", DIGEST, record_nodes)

    # Full proof for record 0 / subject.age (an int present value).
    target_record, target_path = 0, "subject.age"
    target_pos = next(i for i, s in enumerate(specs) if s.path == target_path)
    field_levels = records_json[target_record]["_field_levels"]
    field_sibs = authentication_path(field_levels, target_pos)
    record_sibs = authentication_path(record_levels, target_record)
    sec = secret_material[(target_record, target_path)]
    proof = {
        "protocol_version": "audit-commit-v1",
        "digest": DIGEST,
        "batch_id": FIXTURE_BATCH_ID,
        "batch_root_hex": batch_root.hex(),
        "record_count": len(records),
        "field_count": len(specs),
        "claim": {
            "record_index": target_record,
            "position": target_pos,
            "path": target_path,
            "field_type": "int",
            "state": STATE_PRESENT,
            "commitment_hex": next(
                f["commitment_hex"]
                for f in records_json[target_record]["fields"]
                if f["position"] == target_pos
            ),
        },
        "field_tree": {
            "leaf_count": len(specs),
            "siblings_hex": [None if s is None else s.hex() for s in field_sibs],
            "record_root_hex": records_json[target_record]["record_root_hex"],
        },
        "record_tree": {
            "leaf_count": len(records),
            "siblings_hex": [None if s is None else s.hex() for s in record_sibs],
        },
        "reveal": {"value": sec["value"], "salt_hex": sec["salt_hex"]},
    }

    ok, category, reason, steps = independent_verify(
        proof, batch_root.hex(), expected_path=target_path, expected_record=0
    )

    golden = {
        "generator": "scripts/generate_golden.py",
        "digest": DIGEST,
        "batch_id": FIXTURE_BATCH_ID,
        "salt_derivation": "deterministic (see app/parsing/fixtures.py)",
        "batch_root_hex": batch_root.hex(),
        "records": [
            {
                "record_index": r["record_index"],
                "record_root_hex": r["record_root_hex"],
                "fields": r["fields"],
            }
            for r in records_json
        ],
        "sample_proof": proof,
        "independent_verifier_result": {
            "valid": ok,
            "category": category,
            "reason": reason,
            "steps": steps,
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(golden, indent=2, ensure_ascii=False, sort_keys=False))
    print(f"wrote {OUT}")
    print(f"batch_root = {batch_root.hex()}")
    print(f"independent verify: valid={ok} category={category} reason={reason}")
    if not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
