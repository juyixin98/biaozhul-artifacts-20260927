"""Cross-implementation agreement tests.

Answers come from TWO independently written implementations:

* ``app.core`` - the service kernel (uses the ``cryptography`` package);
* ``independent.verifier`` - standard-library-only reimplementation that does
  not import the kernel.

Tests build proofs with the kernel, then require the independent verifier to
reach the same verdict and category on valid proofs and on every mutation.
Neither side is allowed to be the sole source of expected answers.
"""
from __future__ import annotations

import copy

from app.core.batch import build_batch, disclose_field, verify_proof
from app.parsing import parse_field_specs, parse_records
from app.security.saltpolicy import SaltPolicy
from independent.verifier import independent_verify

DIGEST = "sha256"


def _build():
    fields = parse_field_specs(
        [
            {"path": "a.name", "type": "text"},
            {"path": "a.age", "type": "int"},
            {"path": "a.vip", "type": "bool", "value_space": 2},
            {"path": "a.note", "type": "text"},
        ]
    )
    records = parse_records(
        [
            {
                "a.name": "Same Value",
                "a.age": 40,
                "a.vip": True,
                "a.note": {"state": "null"},
            },
            {"a.name": "Same Value", "a.age": 9, "a.vip": False},  # note missing
        ],
        fields,
    )
    return build_batch(
        batch_id="X1", fields=fields, records=records, policy=SaltPolicy(DIGEST)
    )


def _both(proof, root, **kw):
    kv = verify_proof(proof, trusted_batch_root_hex=root, **kw)
    iv_kw = {
        "expected_path": kw.get("expected_path"),
        "expected_record": kw.get("expected_record_index"),
    }
    ok, cat, reason, steps = independent_verify(proof, root, **iv_kw)
    return kv, (ok, cat, reason, steps)


def test_independent_verifier_accepts_every_valid_proof():
    batch = _build()
    root = batch.batch_root_hex
    for rec, path, state in [
        (0, "a.name", "present"),
        (0, "a.age", "present"),
        (0, "a.vip", "present"),
        (0, "a.note", "null"),
        (1, "a.name", "present"),
        (1, "a.note", "missing"),
    ]:
        proof = disclose_field(batch, rec, path)
        kv, iv = _both(proof, root, expected_path=path, expected_record_index=rec)
        assert kv.valid is True, kv.reason
        assert iv[0] is True, iv[2]
        assert kv.claim["state"] == state


def test_implementations_agree_on_all_mutations():
    batch = _build()
    root = batch.batch_root_hex
    base = disclose_field(batch, 0, "a.age")

    mutations = []

    wrong_salt = copy.deepcopy(base)
    s = bytes.fromhex(wrong_salt["reveal"]["salt_hex"])
    wrong_salt["reveal"]["salt_hex"] = (bytes([s[0] ^ 1]) + s[1:]).hex()
    mutations.append(("wrong-salt", wrong_salt, "COMMITMENT_MISMATCH"))

    wrong_value = copy.deepcopy(base)
    wrong_value["reveal"]["value"] = 41
    mutations.append(("wrong-value", wrong_value, "COMMITMENT_MISMATCH"))

    wrong_path = copy.deepcopy(base)
    wrong_path["claim"]["path"] = "a.name"
    mutations.append(("wrong-path", wrong_path, "COMMITMENT_MISMATCH"))

    # Fields are canonically ordered by path: here a.age is at position 0,
    # a.name at 1, a.note at 2, a.vip at 3. Rebind a.age to position 3.
    wrong_position = copy.deepcopy(base)
    assert base["claim"]["position"] == 0
    wrong_position["claim"]["position"] = 3
    # Identity rebound but the reveal/salt still belong to a.age at position 0
    # -> the recomputed commitment does not equal the old claimed leaf hash.
    mutations.append(("wrong-position", wrong_position, "COMMITMENT_MISMATCH"))

    # Swap in a NEIGHBOUR's commitment while keeping a.age's value+salt:
    # recomputation under the a.age identity cannot reproduce that leaf.
    neighbour_commit = copy.deepcopy(base)
    neighbour_commit["claim"]["commitment_hex"] = disclose_field(
        batch, 0, "a.vip"
    )["claim"]["commitment_hex"]
    mutations.append(
        ("neighbour-leaf-commitment", neighbour_commit, "COMMITMENT_MISMATCH")
    )

    wrong_root = copy.deepcopy(base)
    mutations.append(
        (
            "wrong-root",
            base,  # proof itself fine; trusted root passed below differs
            "ROOT_MISMATCH",
        )
    )

    bad_sibling = copy.deepcopy(base)
    sib = bad_sibling["field_tree"]["siblings_hex"]
    i = next(j for j, x in enumerate(sib) if x is not None)
    b = bytes.fromhex(sib[i])
    sib[i] = (bytes([b[0] ^ 0x0F]) + b[1:]).hex()
    mutations.append(("bad-sibling", bad_sibling, "MERKLE_PATH_MISMATCH"))

    truncated = copy.deepcopy(base)
    truncated["record_tree"]["siblings_hex"] = []
    mutations.append(("truncated-path", truncated, "MERKLE_PATH_MISMATCH"))

    other_record_request = disclose_field(batch, 1, "a.age")
    mutations.append(
        (
            "wrong-record-request",
            other_record_request,
            "IDENTITY_MISMATCH",
        )
    )

    for name, proof, expected_cat in mutations:
        if name == "wrong-root":
            kv = verify_proof(proof, trusted_batch_root_hex="ee" * 32)
            ok, cat, reason, _ = independent_verify(proof, "ee" * 32)
        elif name == "wrong-record-request":
            kv, (ok, cat, reason, _) = _both(
                proof, root, expected_path="a.age", expected_record_index=0
            )
        else:
            kv, (ok, cat, reason, _) = _both(proof, root)
        assert kv.valid is False, name
        assert ok is False, name
        assert kv.category == expected_cat, (name, kv.category)
        assert cat == expected_cat, (name, cat, reason)


def test_low_entropy_unsalted_is_enumerable_salted_is_not():
    """Demonstrate the documented low-entropy limitation concretely.

    An UNSALTED boolean commitment can be identified by trying both possible
    values; a salted one cannot (a guess without the salt does not match).
    """
    import hashlib

    # Mirror the field commitment framing for a tiny offline dictionary attack.
    # (The attacker knows batch/record/position/path/type but not the salt.)
    def unsalted(v: bool) -> bytes:
        from app.core.commitment import field_commitment

        return field_commitment(
            digest_name=DIGEST,
            batch_id="X1",
            record_index=0,
            position=2,
            path="a.vip",
            field_type="bool",
            state="present",
            value=v,
            salt=b"",  # unsalted (policy refusal happens at service boundary)
        )

    target_true = unsalted(True)
    assert any(unsalted(g) == target_true for g in (True, False))

    # The real batch used a random salt: neither guess without the salt hits.
    batch = _build()
    real = next(
        f.commitment_hex
        for f in batch.records[0].fields
        if f.path == "a.vip"
    )
    assert unsalted(True).hex() != real
    assert unsalted(False).hex() != real
