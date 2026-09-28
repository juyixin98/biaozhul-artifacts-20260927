"""Local synthetic fixtures — no real business data, no external accounts.

The dataset models a tiny local-audit batch of expense records. It deliberately
includes every edge case the acceptance criteria name:
  * two different fields carrying the SAME value (same across records too),
  * a null value, an empty string, and a field that is entirely MISSING,
  * one explicitly UNSALTED low-entropy field (status),
  * typed values spanning string/int/decimal/bool/date/timestamp.
"""
from __future__ import annotations

import copy

SCHEMA = [
    {"name": "merchant", "type": "string", "salted": True},
    {"name": "amount", "type": "decimal", "salted": True},
    {"name": "quantity", "type": "int", "salted": True},
    {"name": "approved", "type": "bool", "salted": True},
    {"name": "invoice_date", "type": "date", "salted": True},
    {"name": "submitted_at", "type": "timestamp", "salted": True},
    {"name": "note", "type": "string", "salted": True},
    {"name": "status", "type": "string", "salted": False},  # enumerable by design
]

RECORDS = [
    {
        # record 0: two fields share the value "Blue" via merchant and note
        "merchant": "Blue Kiosk",
        "amount": "12.30",          # trailing zero must survive
        "quantity": 3,
        "approved": True,
        "invoice_date": "2026-03-01",
        "submitted_at": "2026-03-01T09:00:00+00:00",
        "note": "",                 # empty string
        "status": "PAID",
    },
    {
        # record 1: same merchant string as record 0 (same value, other cell);
        # note is explicitly null; status is absent (missing)
        "merchant": "Blue Kiosk",
        "amount": "12.30",
        "quantity": 3,
        "approved": True,
        "invoice_date": "2026-03-01",
        "submitted_at": "2026-03-01T09:00:00Z",
        "note": None,
        # "status" intentionally missing
    },
    {
        # record 2: a duplicate pair where swapping fields is tempting —
        # quantity=7 and a note whose TEXT is also "7" but typed string
        "merchant": "Cafe Seven",
        "amount": "7",
        "quantity": 7,
        "approved": False,
        "invoice_date": "2026-03-02",
        "submitted_at": "2026-03-02T12:30:00+08:00",
        "note": "7",
        "status": "PAID",
    },
]


def schema():
    return copy.deepcopy(SCHEMA)


def records():
    return copy.deepcopy(RECORDS)
