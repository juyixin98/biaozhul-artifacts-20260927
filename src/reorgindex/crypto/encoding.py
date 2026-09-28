"""Canonical wire encoding.

Every hash and signature in this project is computed over a *canonical JSON*
byte string so that a Python dict reconstructed from an HTTP body hashes
identically to the bytes the sender signed.  Rules:

* JSON object keys are sorted lexicographically (``sort_keys=True``);
* no insignificant whitespace (``separators=(",", ":")``);
* hexadecimal hash/key fields travel as lower-case strings;
* integers are never used for amounts transported over the wire -- amounts are
  decimal strings so there is no precision or large-int ambiguity.
"""
from __future__ import annotations

import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
