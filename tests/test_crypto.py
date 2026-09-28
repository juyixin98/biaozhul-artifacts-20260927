"""Crypto-layer tests: signatures produced by the independent oracle and
verified through the core; threshold boundary math; membership rules."""

import pytest

from lc import encoding
from lc.crypto import (
    threshold_weight,
    verify_certificate,
    verify_individual,
)
from lc.errors import Code, LightClientError
from lc.types import Certificate, Committee, Header
from helpers import x32


# --- threshold boundary ----------------------------------------------------
@pytest.mark.parametrize(
    "total,required",
    [(1, 1), (2, 2), (3, 3), (4, 3), (10, 7), (30, 21), (60, 41), (50, 34)],
)
def test_threshold_formula(total, required):
    assert threshold_weight(total) == required
    # strictly greater than 2/3: required-1 must be <= 2W/3
    assert (required - 1) * 3 <= 2 * total
    assert required * 3 > 2 * total


def test_threshold_rejects_nonpositive():
    with pytest.raises(ValueError):
        threshold_weight(0)


# --- end-to-end verify with oracle-generated signatures --------------------
def _cp_objects(golden):
    header = Header.from_dict(golden["checkpoint"]["header"])
    committee = Committee.from_dict(
        golden["checkpoint"]["committee"], max_size=256
    )
    return header, committee


def test_oracle_signatures_verify(golden):
    header, committee = _cp_objects(golden)
    root = encoding.header_root(header)
    cert = Certificate.from_dict(golden["checkpoint"]["certificate"])
    result = verify_certificate(cert, committee, root)
    assert result.verified is True
    assert result.signed_weight == 50
    assert result.required_weight == 41
    assert result.distinct_signers == 5


def test_underweight_cert_returns_weights(golden):
    vec = _single(golden, "weight_below_threshold")
    committee = Committee.from_dict(
        golden["checkpoint"]["committee"], max_size=256
    )
    root = x32(vec["root"])
    cert = Certificate.from_dict(vec["certificate"])
    result = verify_certificate(cert, committee, root)
    assert result.verified is False
    assert result.signed_weight == 40
    assert result.required_weight == 41
    assert result.failure_code == Code.INSUFFICIENT_WEIGHT.value


def test_unknown_signer_old_committee(golden):
    # Build post-rotation active committee (cmt5); cert signed by base keys.
    rot = _single(golden, "committee_rotation_authorized")
    new_committee = Committee.from_dict(rot["next_committee"], max_size=256)
    post = _single(golden, "old_committee_signs_after_rotation")
    root = x32(post["root"])
    cert = Certificate.from_dict(post["certificate"])
    with pytest.raises(LightClientError) as ei:
        verify_certificate(cert, new_committee, root)
    assert ei.value.code is Code.SIGNER_UNKNOWN


def test_corrupt_signature_is_crypto_failure(golden):
    vec = _single(golden, "bad_signature")
    committee = Committee.from_dict(
        golden["checkpoint"]["committee"], max_size=256
    )
    root = x32(vec["root"])
    cert = Certificate.from_dict(vec["certificate"])
    with pytest.raises(LightClientError) as ei:
        verify_certificate(cert, committee, root)
    assert ei.value.code is Code.CRYPTO_BAD_SIGNATURE


def test_wrong_message_does_not_verify():
    # An oracle signature over root A must not verify against a different root.
    pk = bytes.fromhex("02" * 32)
    assert verify_individual(pk, b"\x00" * 64, encoding.certificate_message(b"r" * 32)) is False


# --- helpers ---------------------------------------------------------------
def _single(golden, vec_id):
    for v in golden["vectors"]:
        if v["id"] == vec_id:
            return v
    raise KeyError(vec_id)
