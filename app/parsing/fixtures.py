"""Local synthetic fixtures.

No production accounts and no real business data: every record here is
generated locally. The fixture is deliberately rich in the edge cases the
acceptance tests target:

* two different field paths carrying the *same* value (``name_primary`` and
  ``name_secondary`` are both "Liu Ming"),
* field-position swapping material (adjacent text fields),
* an explicit null and an explicit missing in the same record,
* a low-entropy boolean field,
* typed values across all six supported types.

Set ``fixed_salt_seed`` to derive salts deterministically (used by the golden
vector / cross-check tooling). Leave it ``None`` for production-style random
salts.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.security.saltpolicy import MIN_SALT_BYTES

FIXTURE_BATCH_ID = "batch-synthetic-0001"

FIXTURE_FIELDS: list[dict[str, Any]] = [
    {"path": "subject.id", "type": "text"},
    {"path": "subject.name_primary", "type": "text"},
    {"path": "subject.name_secondary", "type": "text"},
    {"path": "subject.age", "type": "int"},
    {"path": "subject.is_adult", "type": "bool", "value_space": 2},
    {"path": "subject.score", "type": "decimal"},
    {"path": "subject.birth_date", "type": "date"},
    {"path": "subject.audited_at", "type": "timestamp"},
    {"path": "subject.remark", "type": "text"},
    {"path": "subject.optional_code", "type": "text"},
]

FIXTURE_RECORDS: list[dict[str, Any]] = [
    {
        # Same value under two different field names -> commitments must
        # still differ because path+position are bound.
        "subject.id": "PERSON-0001",
        "subject.name_primary": "Liu Ming",
        "subject.name_secondary": "Liu Ming",
        "subject.age": 29,
        "subject.is_adult": True,
        "subject.score": "87.50",
        "subject.birth_date": "1997-04-12",
        "subject.audited_at": "2026-09-27T10:15:43+08:00",
        "subject.remark": "",          # empty string is a PRESENT value
        "subject.optional_code": {"state": "null"},
    },
    {
        "subject.id": "PERSON-0002",
        "subject.name_primary": "Chen Wei",
        # name_secondary deliberately omitted -> missing (not null)
        "subject.age": 17,
        "subject.is_adult": False,
        "subject.score": "0",
        "subject.birth_date": "2009-01-30",
        "subject.audited_at": "2026-09-27T02:15:43Z",  # same instant as rec1
        "subject.remark": {"state": "missing"},
        "subject.optional_code": "X-7",
    },
]


def fixture_payload() -> dict[str, Any]:
    return {
        "batch_id": FIXTURE_BATCH_ID,
        "fields": FIXTURE_FIELDS,
        "records": FIXTURE_RECORDS,
    }


def deterministic_salt(
    batch_id: str, record_index: int, position: int, path: str
) -> bytes:
    """Derive a repeatable 16-byte salt for golden vectors.

    This is ONLY for offline reference tooling; the service path always uses
    the CSPRNG. The key material is a fixed local string -- no secret is
    involved or implied.
    """
    seed = (
        f"golden-vector-salt-derivation|{batch_id}|{record_index}|"
        f"{position}|{path}".encode()
    )
    out = b""
    counter = 0
    while len(out) < MIN_SALT_BYTES:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:MIN_SALT_BYTES]
