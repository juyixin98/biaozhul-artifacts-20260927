"""Kernel recovery tests.

Covers the required verification matrix with *specific* result/failure
assertions:

* enumerate every valid threshold subset of a small config -> same secret
* below threshold is categorically rejected
* shares from a mixed collection are rejected (set-identity binding)
* duplicate x is counted once; conflicting duplicate x is flagged
* a corrupted share fails the independent integrity check
* shares over a different field/threshold are rejected as parameter-incompatible
* recovery failure does NOT identify every malicious party (trust boundary)
"""
from __future__ import annotations

import copy
import dataclasses
import itertools

from app.core.field import FieldParams, SECP256K1_P
from app.core.kernel import Kernel
from app.core.shamir import RecoverStatus
from app.parsing import RejectReason
from conftest import SECRET_A, SECRET_LONG, make_collection


def _shares(created):
    return created["shares"]


# --------------------------------------------------------------------------- #
# Enumerate every legal threshold subset -> identical secret
# --------------------------------------------------------------------------- #
def test_every_threshold_subset_recovers_same_secret(kernel: Kernel):
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_enum")
    shares = _shares(out)
    for combo in itertools.combinations(range(5), 3):
        subset = [shares[i] for i in combo]
        report = kernel.recover(
            request_id=f"req_subset_{combo}",
            collection_id="coll_enum",
            submitted=subset,
        )
        assert report.status == RecoverStatus.RECOVERED_UNVERIFIABLE
        assert report.secret == SECRET_A
        assert sorted(report.used_xs) == sorted(i + 1 for i in combo)
        assert report.rejected == []


def test_every_larger_than_threshold_subset_is_verified(kernel: Kernel):
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_verified")
    shares = _shares(out)
    for size in (4, 5):
        for combo in itertools.combinations(range(5), size):
            subset = [shares[i] for i in combo]
            report = kernel.recover(
                request_id="req", collection_id="coll_verified", submitted=subset
            )
            assert report.status == RecoverStatus.RECOVERED_VERIFIED
            assert report.secret == SECRET_A
            assert set(report.extra_xs) == set(i + 1 for i in combo) - set(report.used_xs)


def test_multiblock_secret_all_subsets(kernel: Kernel):
    out = make_collection(kernel, SECRET_LONG, t=2, n=4, cid="coll_long")
    shares = _shares(out)
    assert out["block_count"] == 3
    for combo in itertools.combinations(range(4), 2):
        report = kernel.recover(
            request_id="req", collection_id="coll_long",
            submitted=[shares[i] for i in combo],
        )
        assert report.status == RecoverStatus.RECOVERED_UNVERIFIABLE
        assert report.secret == SECRET_LONG


# --------------------------------------------------------------------------- #
# Below threshold -> categorical reject
# --------------------------------------------------------------------------- #
def test_below_threshold_rejected(kernel: Kernel):
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_low")
    shares = _shares(out)
    report = kernel.recover(
        request_id="req_low", collection_id="coll_low", submitted=shares[:2]
    )
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT
    assert report.secret is None
    assert len(report.distinct_xs) == 2


def test_duplicate_shares_do_not_inflate_count(kernel: Kernel):
    # threshold 3 but only TWO distinct shares, one repeated -> still rejected.
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_dup")
    shares = _shares(out)
    submitted = [shares[0], shares[1], copy.deepcopy(shares[0])]
    report = kernel.recover(
        request_id="req_dup", collection_id="coll_dup", submitted=submitted
    )
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT
    assert report.distinct_xs == [1, 2]
    reasons = {r["reason"] for r in report.rejected}
    assert RejectReason.DUPLICATE_X.value in reasons


def test_conflicting_duplicate_x_is_flagged_not_preferred(kernel: Kernel):
    # Two MAC-valid shares with the SAME x but different y (issued by the key
    # holder). Neither may silently win; the conflict is reported.
    out = make_collection(kernel, SECRET_A, t=2, n=3, cid="coll_conf")
    shares = _shares(out)
    rogue = kernel.issue_share_with_value(
        "coll_conf", x=1, ys=tuple((int(y) + 1) % SECP256K1_P for y in shares[0]["ys"])
    )
    submitted = [shares[0], shares[1], rogue.to_dict()]
    report = kernel.recover(
        request_id="req_conf", collection_id="coll_conf", submitted=submitted
    )
    # x=2 plus one copy of x=1 -> two distinct admissible shares reaches t=2.
    assert report.status in {
        RecoverStatus.RECOVERED_UNVERIFIABLE,
        RecoverStatus.REJECTED_INCONSISTENT,
    }
    conflict = [r for r in report.rejected
                if r["reason"] == RejectReason.DUPLICATE_X_CONFLICT.value]
    assert len(conflict) == 1


# --------------------------------------------------------------------------- #
# Independent integrity: tampered share rejected with the MAC category
# --------------------------------------------------------------------------- #
def test_tampered_share_fails_integrity(kernel: Kernel):
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_mac")
    shares = _shares(out)
    tampered = copy.deepcopy(shares[2])
    tampered["ys"][0] = str((int(tampered["ys"][0]) + 1) % SECP256K1_P)
    report = kernel.recover(
        request_id="req_mac", collection_id="coll_mac",
        submitted=[shares[0], shares[1], tampered],
    )
    # only 2 MAC-valid shares -> below threshold, and the tampered one is
    # specifically categorised as an integrity failure.
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT
    mac_rejects = [r for r in report.rejected
                   if r["reason"] == RejectReason.BAD_INTEGRITY.value]
    assert len(mac_rejects) == 1
    assert mac_rejects[0]["x"] == 3
    assert report.secret is None


def test_tampered_above_threshold_is_detected_via_consistency(kernel: Kernel):
    # t=3, 5 shares, one MAC-valid share carries a bad y, with redundancy the
    # set is detected as inconsistent.
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_cons")
    shares = _shares(out)
    rogue = kernel.issue_share_with_value(
        "coll_cons", x=4,
        ys=tuple((int(y) + 999) % SECP256K1_P for y in shares[3]["ys"]),
    )
    submitted = [shares[0], shares[1], shares[2], rogue.to_dict(), shares[4]]
    report = kernel.recover(
        request_id="req_cons", collection_id="coll_cons", submitted=submitted
    )
    assert report.status == RecoverStatus.REJECTED_INCONSISTENT
    assert report.secret is None
    assert 4 in report.mismatched_xs


# --------------------------------------------------------------------------- #
# Mixed collection + parameter/field incompatibility
# --------------------------------------------------------------------------- #
def test_share_from_other_collection_rejected(kernel: Kernel):
    a = make_collection(kernel, SECRET_A, t=2, n=3, cid="coll_A")
    b = make_collection(kernel, b"different secret!!", t=2, n=3, cid="coll_B")
    # submit B's share while recovering A
    submitted = [a["shares"][0], b["shares"][1]]
    report = kernel.recover(
        request_id="req_mix", collection_id="coll_A", submitted=submitted
    )
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT
    reasons = {r["reason"] for r in report.rejected}
    assert RejectReason.WRONG_COLLECTION.value in reasons


def test_foreign_field_parameters_rejected(kernel: Kernel):
    out = make_collection(kernel, SECRET_A, t=2, n=3, cid="coll_fp")
    foreign = copy.deepcopy(out["shares"][1])
    foreign["field"] = {
        "version": "gf-secp256k1-v1",
        "prime": str(SECP256K1_P + 42),          # different prime
        "prime_bits": 256,
        "chunk_bytes": 31,
    }
    report = kernel.recover(
        request_id="req_fp", collection_id="coll_fp",
        submitted=[out["shares"][0], foreign],
    )
    reasons = {r["reason"] for r in report.rejected}
    assert RejectReason.FIELD_INCOMPATIBLE.value in reasons
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT


def test_threshold_binding_mismatch_rejected(kernel: Kernel):
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_bind")
    forged_meta = copy.deepcopy(out["shares"][1])
    # Claim a different (t,n) binding; body MAC is over the true binding.
    forged_meta["threshold"] = 2
    forged_meta["total"] = 5
    # parse binding check happens before/instead of MAC, but either way it must
    # never be admitted. Assert it is rejected and the secret is not recovered.
    report = kernel.recover(
        request_id="req_bind", collection_id="coll_bind",
        submitted=[out["shares"][0], forged_meta, out["shares"][2]],
    )
    reasons = {r["reason"] for r in report.rejected}
    assert (
        RejectReason.PARAMETER_MISMATCH.value in reasons
        or RejectReason.BAD_INTEGRITY.value in reasons
    )
    # Not all 3 submitted shares are admissible -> cannot recover.
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT


def test_malformed_share_rejected(kernel: Kernel):
    out = make_collection(kernel, SECRET_A, t=2, n=3, cid="coll_mal")
    bad = {"collection_id": "coll_mal", "x": 1}  # missing fields
    report = kernel.recover(
        request_id="req_mal", collection_id="coll_mal",
        submitted=[out["shares"][0], bad],
    )
    reasons = {r["reason"] for r in report.rejected}
    assert RejectReason.MALFORMED.value in reasons
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT


def test_block_count_mismatch_rejected(kernel: Kernel):
    # A one-block secret collection; forge a share envelope claiming extra block.
    out = make_collection(kernel, SECRET_A, t=2, n=3, cid="coll_blk")
    rogue = kernel.issue_share_with_value(
        "coll_blk", x=2, ys=(12345, 67890)  # two blocks
    )
    report = kernel.recover(
        request_id="req_blk", collection_id="coll_blk",
        submitted=[out["shares"][0], rogue.to_dict()],
    )
    reasons = {r["reason"] for r in report.rejected}
    assert RejectReason.BLOCK_COUNT_MISMATCH.value in reasons
    assert report.status == RecoverStatus.REJECTED_INSUFFICIENT


# --------------------------------------------------------------------------- #
# The key claim: recovery failure != locating all malicious parties
# --------------------------------------------------------------------------- #
def test_failure_does_not_identify_all_malicious_parties(kernel: Kernel):
    # With exactly the threshold of MAC-valid shares, a malicious key-holder can
    # supply a consistent-looking but wrong share and the server cannot point
    # the finger at a specific party. Here the baseline subset excludes the
    # rogue so the *only* thing the server can say is "inconsistent set".
    out = make_collection(kernel, SECRET_A, t=3, n=5, cid="coll_attr")
    shares = _shares(out)
    # Block layout (32-byte big-endian): [len][19 content bytes][12 zero pad].
    # The final *content* byte is 12 bytes above the bottom, so this weight
    # changes real content rather than trailing zero padding.
    delta = 1 << (12 * 8)
    rogue = kernel.issue_share_with_value(
        "coll_attr", x=1,
        ys=tuple((int(y) + delta) % SECP256K1_P for y in shares[0]["ys"]),
    )
    # threshold subset containing the rogue, with two honest shares: the
    # reconstructed constant is wrong but no extra share exists to cross-check.
    submitted = [rogue.to_dict(), shares[1], shares[2]]
    report = kernel.recover(
        request_id="req_attr", collection_id="coll_attr", submitted=submitted
    )
    # The server either returns a (wrong-looking) unverifiable value or rejects
    # on decode; in NEITHER case does it prove which party is malicious.
    assert report.status in {
        RecoverStatus.RECOVERED_UNVERIFIABLE,
        RecoverStatus.REJECTED_INCONSISTENT,
    }
    assert report.mismatched_xs == []  # nothing extra to implicate a party
    if report.status == RecoverStatus.RECOVERED_UNVERIFIABLE:
        # the unverifiable output must not be mistaken for the true secret
        assert report.secret != SECRET_A


def test_unknown_collection_raises(kernel: Kernel):
    import pytest
    from app.state import CollectionNotFound
    with pytest.raises(CollectionNotFound):
        kernel.recover(
            request_id="req_x", collection_id="nope",
            submitted=[],
        )
