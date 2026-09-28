"""Acceptance tests against the pure cryptographic kernel.

Every assertion names the concrete expected result and, for failures, the
exact failure category -- never just "the call worked".
"""
from __future__ import annotations

import copy

import pytest

from app.core.batch import (
    FieldSpec,
    build_batch,
    disclose_field,
    verify_proof,
)
from app.core.encoding import (
    STATE_MISSING,
    STATE_NULL,
    STATE_PRESENT,
    encode_present,
)
from app.core.errors import TypeEncodingError
from app.security.saltpolicy import SaltPolicy
from app.parsing import parse_field_specs, parse_records

DIGEST = "sha256"
POLICY = SaltPolicy(digest_name=DIGEST)


def _fields_raw() -> list[dict]:
    return [
        {"path": "subject.a_name", "type": "text"},
        {"path": "subject.b_name", "type": "text"},
        {"path": "subject.age", "type": "int"},
        {"path": "subject.is_adult", "type": "bool", "value_space": 2},
        {"path": "subject.score", "type": "decimal"},
        {"path": "subject.remark", "type": "text"},
        {"path": "subject.code", "type": "text"},
    ]


def _records_raw() -> list[dict]:
    return [
        {
            "subject.a_name": "Liu Ming",
            "subject.b_name": "Liu Ming",  # same value, different path
            "subject.age": 29,
            "subject.is_adult": True,
            "subject.score": "87.50",
            "subject.remark": "",        # empty string present
            "subject.code": {"state": "null"},
        },
        {
            "subject.a_name": "Chen Wei",
            # b_name omitted -> missing
            "subject.age": 17,
            "subject.is_adult": False,
            "subject.score": "0",
            "subject.remark": {"state": "missing"},
            "subject.code": "X-7",
        },
    ]


def _fields() -> list[FieldSpec]:
    return parse_field_specs(_fields_raw())


def _records() -> list[dict]:
    return parse_records(_records_raw(), _fields())


@pytest.fixture()
def built():
    return build_batch(
        batch_id="B1", fields=_fields(), records=_records(), policy=POLICY
    )


# ---------------------------------------------------------------------------
# 1. Same value under different fields -> different commitments
# ---------------------------------------------------------------------------


def test_same_value_different_fields_different_commitments(built):
    r0 = built.records[0]
    a = next(f for f in r0.fields if f.path == "subject.a_name")
    b = next(f for f in r0.fields if f.path == "subject.b_name")
    assert a.commitment_hex != b.commitment_hex
    # Identical value payload, distinct position and path binding.
    assert a.position != b.position


def test_commitment_changes_when_path_changes():
    c1 = _commit(path="x", value="v", position=0)
    c2 = _commit(path="y", value="v", position=0)
    c3 = _commit(path="x", value="v", position=1)
    assert c1 != c2 != c3 and c1 != c3


def _commit(*, path, value, position, record=0, ftype="text", state="present", salt=b"\x01" * 16):
    from app.core.commitment import field_commitment

    return field_commitment(
        digest_name=DIGEST,
        batch_id="B1",
        record_index=record,
        position=position,
        path=path,
        field_type=ftype,
        state=state,
        value=value,
        salt=salt if state == "present" else None,
    )


# ---------------------------------------------------------------------------
# 2. Field swapping cannot pass verification
# ---------------------------------------------------------------------------


def test_swapped_field_identity_rejected(built):
    proof = disclose_field(built, 0, "subject.a_name")
    root = built.batch_root_hex

    # Present the proof of a_name while claiming it is b_name.
    swapped = copy.deepcopy(proof)
    swapped["claim"]["path"] = "subject.b_name"
    v = verify_proof(swapped, trusted_batch_root_hex=root)
    assert v.valid is False
    # Rebinding path changes the commitment; the claimed leaf no longer matches
    # a recomputation under the forged identity.
    assert v.category == "COMMITMENT_MISMATCH"

    # Swap position only.
    swapped2 = copy.deepcopy(proof)
    swapped2["claim"]["position"] = proof["claim"]["position"] + 1
    v2 = verify_proof(swapped2, trusted_batch_root_hex=root)
    assert v2.valid is False
    assert v2.category == "COMMITMENT_MISMATCH"

    # A proof for b_name cannot satisfy a request that pins a_name.
    proof_b = disclose_field(built, 0, "subject.b_name")
    v3 = verify_proof(
        proof_b, trusted_batch_root_hex=root, expected_path="subject.a_name"
    )
    assert v3.valid is False
    assert v3.category == "IDENTITY_MISMATCH"

    # And a proof from record 1 cannot be presented as record 0.
    v4 = verify_proof(
        disclose_field(built, 1, "subject.age"),
        trusted_batch_root_hex=root,
        expected_record_index=0,
    )
    assert v4.valid is False
    assert v4.category == "IDENTITY_MISMATCH"


# ---------------------------------------------------------------------------
# 3. Empty string vs null vs missing are distinct and verify correctly
# ---------------------------------------------------------------------------


def test_empty_string_present_is_distinct_from_null_and_missing(built):
    r0 = built.records[0]
    remark = next(f for f in r0.fields if f.path == "subject.remark")  # "" present
    code = next(f for f in r0.fields if f.path == "subject.code")  # null
    assert remark.state == STATE_PRESENT
    assert code.state == STATE_NULL
    assert remark.commitment_hex != code.commitment_hex

    r1 = built.records[1]
    b_name = next(f for f in r1.fields if f.path == "subject.b_name")  # missing
    remark1 = next(f for f in r1.fields if f.path == "subject.remark")  # missing
    assert b_name.state == STATE_MISSING and remark1.state == STATE_MISSING


def test_valid_proofs_for_present_null_and_missing(built):
    root = built.batch_root_hex

    p_present = disclose_field(built, 0, "subject.remark")  # ""
    v = verify_proof(p_present, trusted_batch_root_hex=root, expected_path="subject.remark")
    assert v.valid is True
    assert v.claim["state"] == STATE_PRESENT
    assert v.claim["value"] == ""

    p_null = disclose_field(built, 0, "subject.code")
    v_null = verify_proof(p_null, trusted_batch_root_hex=root, expected_path="subject.code")
    assert v_null.valid is True
    assert v_null.claim["state"] == STATE_NULL
    assert v_null.claim["value"] is None

    p_missing = disclose_field(built, 1, "subject.b_name")
    v_miss = verify_proof(
        p_missing, trusted_batch_root_hex=root, expected_path="subject.b_name"
    )
    assert v_miss.valid is True
    assert v_miss.claim["state"] == STATE_MISSING
    assert p_missing["reveal"] is None


def test_missing_proof_must_carry_no_salt_or_value(built):
    p = disclose_field(built, 1, "subject.b_name")
    tampered = copy.deepcopy(p)
    tampered["reveal"] = {"value": "leak", "salt_hex": "00" * 16}
    v = verify_proof(tampered, trusted_batch_root_hex=built.batch_root_hex)
    assert v.valid is False
    assert v.category == "PROOF_MALFORMED"


# ---------------------------------------------------------------------------
# 4. Wrong salt / wrong value -> COMMITMENT_MISMATCH
# ---------------------------------------------------------------------------


def test_wrong_salt_is_commitment_mismatch(built):
    p = disclose_field(built, 0, "subject.age")
    bad = copy.deepcopy(p)
    raw = bytes.fromhex(bad["reveal"]["salt_hex"])
    flipped = bytes([raw[0] ^ 0x01]) + raw[1:]
    bad["reveal"]["salt_hex"] = flipped.hex()
    v = verify_proof(bad, trusted_batch_root_hex=built.batch_root_hex)
    assert v.valid is False
    assert v.category == "COMMITMENT_MISMATCH"
    assert "commitment" in v.reason.lower()


def test_wrong_value_is_commitment_mismatch(built):
    p = disclose_field(built, 0, "subject.age")
    bad = copy.deepcopy(p)
    bad["reveal"]["value"] = 30  # true value is 29
    v = verify_proof(bad, trusted_batch_root_hex=built.batch_root_hex)
    assert v.valid is False
    assert v.category == "COMMITMENT_MISMATCH"


def test_forging_claimed_commitment_fails_at_commitment_step(built):
    # Swapping in a foreign leaf hash: recomputation under the claimed
    # identity disagrees, so COMMITMENT_MISMATCH (checked before Merkle).
    p = disclose_field(built, 0, "subject.age")
    bad = copy.deepcopy(p)
    bad["claim"]["commitment_hex"] = "00" * 32
    v = verify_proof(bad, trusted_batch_root_hex=built.batch_root_hex)
    assert v.valid is False
    assert v.category == "COMMITMENT_MISMATCH"


def test_forging_record_root_fails_merkle_step(built):
    # Keep commitment + field path consistent but forge the record root the
    # field path supposedly anchors to -> field Merkle path cannot match.
    p = disclose_field(built, 0, "subject.age")
    bad = copy.deepcopy(p)
    bad["field_tree"]["record_root_hex"] = "00" * 32
    v = verify_proof(bad, trusted_batch_root_hex=built.batch_root_hex)
    assert v.valid is False
    assert v.category == "MERKLE_PATH_MISMATCH"


# ---------------------------------------------------------------------------
# 5. Wrong batch root -> ROOT_MISMATCH
# ---------------------------------------------------------------------------


def test_wrong_root_is_root_mismatch(built):
    p = disclose_field(built, 0, "subject.age")
    wrong = ("ff" if p["batch_root_hex"][:2] != "ff" else "00") + p["batch_root_hex"][2:]
    v = verify_proof(p, trusted_batch_root_hex=wrong)
    assert v.valid is False
    assert v.category == "ROOT_MISMATCH"
    assert "trusted" in v.reason.lower()


def test_tampered_sibling_is_merkle_mismatch(built):
    p = disclose_field(built, 0, "subject.age")
    bad = copy.deepcopy(p)
    sibs = bad["field_tree"]["siblings_hex"]
    idx = next(i for i, s in enumerate(sibs) if s is not None)
    s = bytes.fromhex(sibs[idx])
    sibs[idx] = (bytes([s[0] ^ 0xFF]) + s[1:]).hex()
    v = verify_proof(bad, trusted_batch_root_hex=built.batch_root_hex)
    assert v.valid is False
    assert v.category == "MERKLE_PATH_MISMATCH"


# ---------------------------------------------------------------------------
# 6. Typed canonical encoding: type confusion and malformed inputs rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ftype,value",
    [
        ("int", True),       # bool must not encode as int
        ("int", 1.5),
        ("bool", "true"),
        ("decimal", 1.25),   # float rejected (must be string)
        ("decimal", "NaN"),
        ("decimal", "1e3"),
        ("date", "2026-1-2"),
        ("date", "2026-02-30"),
        ("timestamp", "2026-09-27T10:00:00"),  # naive, no timezone
        ("text", 123),
    ],
)
def test_type_encoding_rejects_bad_inputs(ftype, value):
    with pytest.raises(TypeEncodingError):
        encode_present(ftype, value)


def test_decimal_canonicalisation():
    assert encode_present("decimal", "87.50")[1] == b"87.5"
    assert encode_present("decimal", "007.00")[1] == b"7"
    assert encode_present("decimal", "-0.0")[1] == b"0"


def test_timestamp_normalises_timezone_to_utc():
    p1 = encode_present("timestamp", "2026-09-27T10:15:43+08:00")[1]
    p2 = encode_present("timestamp", "2026-09-27T02:15:43Z")[1]
    assert p1 == p2


def test_same_lexical_value_different_type_different_commitment():
    from app.core.commitment import field_commitment

    c_text = field_commitment(
        digest_name=DIGEST, batch_id="B", record_index=0, position=0,
        path="f", field_type="text", state=STATE_PRESENT, value="42",
        salt=b"\x02" * 16,
    )
    c_int = field_commitment(
        digest_name=DIGEST, batch_id="B", record_index=0, position=0,
        path="f", field_type="int", state=STATE_PRESENT, value=42,
        salt=b"\x02" * 16,
    )
    assert c_text != c_int


# ---------------------------------------------------------------------------
# 7. Malformed proofs never succeed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mut",
    [
        lambda p: p.update(protocol_version="other") or p,
        lambda p: p.update(digest="md5") or p,
        lambda p: p["claim"].pop("path") or p,
        lambda p: p["field_tree"].update(siblings_hex=[]) or p,
    ],
)
def test_malformed_proofs_are_classified(built, mut):
    p = copy.deepcopy(disclose_field(built, 0, "subject.age"))
    bad = mut(p)
    v = verify_proof(bad, trusted_batch_root_hex=built.batch_root_hex)
    assert v.valid is False
    assert v.category in {"PROOF_MALFORMED", "MERKLE_PATH_MISMATCH"}
    assert isinstance(v.reason, str) and v.reason
