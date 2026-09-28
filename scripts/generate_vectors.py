#!/usr/bin/env python3
"""Generate FROZEN test vectors using the Python standard library ONLY.

This script deliberately does NOT import app.*. It exists so the expected
digests in tests are produced by an independent implementation of the
published formula rather than by the code under test.

Run:  python scripts/generate_vectors.py
Out:  tests/fixtures/frozen_vectors.json  (checked in)

If the file is regenerated with different (valid) formulas, the protocol
version constants in app/version.py must be bumped first.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import struct
import unicodedata
from decimal import Decimal

COMMITMENT_DOMAIN = b"audit-commit-v1"
MERKLE_DOMAIN = b"audit-merkle-v1"

TAGS = {"string": 0x10, "int": 0x11, "decimal": 0x12, "bool": 0x13,
        "date": 0x14, "timestamp": 0x15, "null": 0x1F}
MISSING = b"\xff\xff"


def lp(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def u64(n: int) -> bytes:
    return struct.pack(">Q", n)


def encode(kind: str, value):
    if kind == "missing":
        return MISSING
    tag = bytes([TAGS[kind]])
    if kind == "null":
        assert value is None
        return tag
    if kind == "string":
        return tag + lp(unicodedata.normalize("NFC", value).encode())
    if kind == "int":
        return tag + struct.pack(">q", value)
    if kind == "decimal":
        d = Decimal(value)
        sign, digits, exp = d.as_tuple()
        coeff = int("".join(map(str, digits)))
        if sign:
            coeff = -coeff
        return tag + struct.pack(">qi", coeff, -exp)
    if kind == "bool":
        return tag + (b"\x01" if value else b"\x00")
    if kind == "date":
        d = dt.date.fromisoformat(value)
        return tag + struct.pack(">hhh", d.year, d.month, d.day)
    if kind == "timestamp":
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        m = dt.datetime.fromisoformat(text).astimezone(dt.timezone.utc)
        micros = int((m - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc))
                     // dt.timedelta(microseconds=1))
        return tag + struct.pack(">q", micros)
    raise ValueError(kind)


def commitment(rec, pos, name, encoded, salt: bytes) -> str:
    material = (COMMITMENT_DOMAIN + lp(name.encode()) + u64(rec) + u64(pos)
                + lp(encoded) + u64(len(salt)) + salt)
    return hashlib.sha256(material).hexdigest()


def leaf(c: str) -> bytes:
    return hashlib.sha256(MERKLE_DOMAIN + b"\x00" + lp(bytes.fromhex(c))).digest()


def node(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(MERKLE_DOMAIN + b"\x01" + lp(a) + lp(b)).digest()


def root(commits: list[str]) -> str:
    if not commits:
        return hashlib.sha256(MERKLE_DOMAIN + b"empty").hexdigest()
    level = [leaf(c) for c in commits]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(node(level[i], level[i + 1]) if i + 1 < len(level)
                       else node(level[i], level[i]))
        level = nxt
    return level[0].hex()


SALT16 = bytes(range(1, 17)).hex()          # 0102...10
SALT16B = bytes(range(17, 33)).hex()       # 1112...20


def main() -> None:
    enc = {
        "decimal_12.30": encode("decimal", "12.30").hex(),
        "decimal_7_str": encode("decimal", "7").hex(),
        "int_7": encode("int", 7).hex(),
        "string_7": encode("string", "7").hex(),
        "string_empty": encode("string", "").hex(),
        "string_blue_kiosk": encode("string", "Blue Kiosk").hex(),
        "bool_true": encode("bool", True).hex(),
        "bool_false": encode("bool", False).hex(),
        "date_2026-03-01": encode("date", "2026-03-01").hex(),
        "ts_zulu": encode("timestamp", "2026-03-01T09:00:00Z").hex(),
        "null": encode("null", None).hex(),
        "missing": encode("missing", None).hex(),
    }

    salt16 = bytes.fromhex(SALT16)
    salt16b = bytes.fromhex(SALT16B)
    commit_cases = {
        "amount_r0_salted": [0, 1, "amount", enc["decimal_12.30"], SALT16],
        "amount_r1_same_value_other_cell": [1, 1, "amount", enc["decimal_12.30"], SALT16B],
        "merchant_r0_salted": [0, 0, "merchant", enc["string_blue_kiosk"], SALT16],
        "merchant_r1_same_value_other_cell": [1, 0, "merchant",
                                              enc["string_blue_kiosk"], SALT16B],
        "note_empty_r0": [0, 6, "note", enc["string_empty"], SALT16],
        "note_null_r1": [1, 6, "note", enc["null"], SALT16B],
        "status_missing_r1": [1, 7, "status", enc["missing"], SALT16B],
        "int7_quantity": [2, 2, "quantity", enc["int_7"], SALT16],
        "string7_note": [2, 6, "note", enc["string_7"], SALT16],
        "status_paid_unsalted": [0, 7, "status",
                                 encode("string", "PAID").hex(), ""],
    }
    commitments = {key: commitment(rec, pos, name, bytes.fromhex(enc_hex),
                                   bytes.fromhex(salt_hex) if salt_hex else b"")
                   for key, (rec, pos, name, enc_hex, salt_hex) in commit_cases.items()}

    # Same-value/different-field must NOT collide, even with the SAME salt:
    cross = commitment(0, 0, "merchant", bytes.fromhex(enc["string_blue_kiosk"]), salt16)
    swapped_name = commitment(0, 6, "note", bytes.fromhex(enc["string_blue_kiosk"]), salt16)
    swapped_pos = commitment(0, 3, "merchant", bytes.fromhex(enc["string_blue_kiosk"]), salt16)
    swapped_record = commitment(1, 0, "merchant", bytes.fromhex(enc["string_blue_kiosk"]), salt16)
    wrong_salt = commitment(0, 0, "merchant", bytes.fromhex(enc["string_blue_kiosk"]), salt16b)

    merkle = {
        "empty": root([]),
        "four_leaves": root([commitments["merchant_r0_salted"],
                             commitments["amount_r0_salted"],
                             commitments["note_empty_r0"],
                             commitments["int7_quantity"]]),
        "three_leaves_odd": root([commitments["merchant_r0_salted"],
                                  commitments["amount_r0_salted"],
                                  commitments["note_null_r1"]]),
        "tampered_last_leaf": root([commitments["merchant_r0_salted"],
                                    commitments["amount_r0_salted"],
                                    commitments["note_null_r1"][:-1] + "0"]),
    }

    out = {
        "generated_by": "scripts/generate_vectors.py (stdlib only)",
        "domains": {"commitment": "audit-commit-v1", "merkle": "audit-merkle-v1"},
        "salts": {"salt16": SALT16, "salt16b": SALT16B},
        "encoded_hex": enc,
        "commitments": commitments,
        "identity_binding": {
            "baseline": cross,
            "name_swapped_to_note": swapped_name,
            "position_swapped": swapped_pos,
            "record_swapped": swapped_record,
            "wrong_salt": wrong_salt,
        },
        "merkle_roots": merkle,
    }
    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(here, "..", "tests", "fixtures", "frozen_vectors.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {os.path.normpath(target)}")


if __name__ == "__main__":
    main()
