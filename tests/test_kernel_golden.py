"""Kernel tests driven by the independent oracle's golden vectors.

Every rejection asserts BOTH:
  * the exact failure ``code`` and top-level ``category`` the oracle predicted,
  * the trusted tip snapshot is byte-identical before vs after the attempt.

Acceptance cases assert the new tip root and active committee.
"""

import pytest

from lc import encoding
from lc.errors import Category, Code, LightClientError
from lc.types import Certificate, Checkpoint, Committee, Header
from helpers import x32


# --------------------------------------------------------------------------- #
# Setup helpers
# --------------------------------------------------------------------------- #
def install(golden, kernel):
    cp = Checkpoint.from_dict(golden["checkpoint"], committee_max_size=256)
    return kernel.install_checkpoint(cp)


def objs(vec):
    header = Header.from_dict(vec["header"])
    cert = Certificate.from_dict(vec["certificate"])
    nc = None
    if vec.get("next_committee"):
        nc = Committee.from_dict(vec["next_committee"], max_size=256)
    return header, cert, nc


def single(golden, vec_id):
    for v in golden["vectors"]:
        if v["id"] == vec_id:
            return v
    raise KeyError(vec_id)


def tip_tuple(kernel):
    t = kernel.store.get_tip()
    return (
        t.tip_header_root,
        t.active_committee_commitment,
        t.tip_round,
        t.tip_timestamp_ms,
        t.sequence,
    )


def expect_reject(golden, kernel, vec_id, *, prerequisite=None):
    vec = single(golden, vec_id)
    if prerequisite:
        prerequisite()
    before = tip_tuple(kernel)
    header, cert, nc = objs(vec)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_header(header, cert, nc)
    err = ei.value
    expected = vec["expected"]
    assert err.code.value == expected["code"], (
        f"{vec_id}: code {err.code.value} != {expected['code']}"
    )
    assert err.category.value == expected["category"], (
        f"{vec_id}: category {err.category.value} != {expected['category']}"
    )
    after = tip_tuple(kernel)
    assert after == before, (
        f"{vec_id}: trusted tip mutated after rejection! "
        f"before={before} after={after}"
    )
    return err


# --------------------------------------------------------------------------- #
# Bootstrapping
# --------------------------------------------------------------------------- #
def test_update_before_checkpoint_is_state_conflict(golden, kernel):
    vec = single(golden, "weight_below_threshold")
    header, cert, _ = objs(vec)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_header(header, cert)
    assert ei.value.code is Code.NOT_INITIALIZED
    assert ei.value.category is Category.STATE_CONFLICT
    assert kernel.store.get_tip().tip_header_root is None


def test_checkpoint_install_once(golden, kernel):
    rep = install(golden, kernel)
    assert rep.accepted is True
    cp_root = x32(golden["checkpoint"]["root"])
    assert kernel.store.get_tip().tip_header_root == cp_root
    # second checkpoint is a state conflict and changes nothing
    before = tip_tuple(kernel)
    with pytest.raises(LightClientError) as ei:
        install(golden, kernel)
    assert ei.value.code is Code.CHECKPOINT_CONFLICT
    assert tip_tuple(kernel) == before


# --------------------------------------------------------------------------- #
# Happy path: continuous legal headers
# --------------------------------------------------------------------------- #
def test_continuous_legal_chain_advances(golden, kernel, clock):
    install(golden, kernel)
    chain = single(golden, "legal_continuous_3")
    cp_root = x32(golden["checkpoint"]["root"])
    parent = cp_root
    for i, item in enumerate(chain["items"]):
        header = Header.from_dict(item["header"])
        cert = Certificate.from_dict(item["certificate"])
        assert encoding.header_root(header) == x32(item["root"])
        assert header.parent_root == parent
        rep = kernel.apply_header(header, cert)
        assert rep.accepted is True
        assert rep.failure_code is None
        assert x32(rep.root) == encoding.header_root(header)
        parent = encoding.header_root(header)
    head = kernel.head()
    assert head["tip_round"] == 103
    assert head["fresh"] is True
    # committee unchanged
    assert (
        head["active_committee_commitment"]
        == golden["checkpoint"]["committee_commitment"]
    )


def test_replay_idempotent_tip_returns_already_known(golden, kernel, clock):
    install(golden, kernel)
    t0 = golden["constants"]["T0"]
    slot = golden["constants"]["SLOT_MS"]
    clock.set(t0 + 2 * slot)  # within trust window of the legal chain
    chain = single(golden, "legal_continuous_3")
    header = Header.from_dict(chain["items"][0]["header"])
    cert = Certificate.from_dict(chain["items"][0]["certificate"])
    kernel.apply_header(header, cert)
    before = tip_tuple(kernel)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_header(header, cert)
    assert ei.value.code is Code.ALREADY_KNOWN
    assert tip_tuple(kernel) == before


# --------------------------------------------------------------------------- #
# Weight threshold boundary
# --------------------------------------------------------------------------- #
def test_insufficient_weight_at_boundary(golden, kernel):
    install(golden, kernel)
    err = expect_reject(golden, kernel, "weight_below_threshold")
    # exact intermediate state recorded: 40 < 41 of 60
    assert err.details["signed_weight"] == 40
    assert err.details["required_weight"] == 41
    assert err.details["total_weight"] == 60


def test_just_over_threshold_after_rotation_accepted(golden, kernel):
    install(golden, kernel)
    rot = single(golden, "committee_rotation_authorized")
    rh, rc, rnc = objs(rot)
    rep = kernel.apply_header(rh, rc, rnc)
    assert rep.accepted is True
    assert rep.active_committee_after == rot["new_committee_commitment"]
    # 4/5 = 40 >= 34
    post = single(golden, "new_committee_authorized")
    ph, pc, _ = objs(post)
    rep2 = kernel.apply_header(ph, pc)
    assert rep2.accepted is True
    assert rep2.cert["signed_weight"] == 40
    assert rep2.cert["required_weight"] == 34


def test_new_committee_underweight(golden, kernel):
    install(golden, kernel)
    rot = single(golden, "committee_rotation_authorized")
    rh, rc, rnc = objs(rot)
    kernel.apply_header(rh, rc, rnc)
    vec = single(golden, "new_committee_underweight")
    before = tip_tuple(kernel)
    ph, pc, _ = objs(vec)
    with pytest.raises(LightClientError) as ei:
        kernel.apply_header(ph, pc)
    assert ei.value.code is Code.INSUFFICIENT_WEIGHT
    assert ei.value.details["signed_weight"] == 30
    assert ei.value.details["required_weight"] == 34
    assert tip_tuple(kernel) == before


# --------------------------------------------------------------------------- #
# Committee rotation must be authorized by the PRIOR committee
# --------------------------------------------------------------------------- #
def test_old_committee_signs_new_header_is_unknown(golden, kernel):
    install(golden, kernel)
    rot = single(golden, "committee_rotation_authorized")
    rh, rc, rnc = objs(rot)
    kernel.apply_header(rh, rc, rnc)
    err = expect_reject(golden, kernel, "old_committee_signs_after_rotation")
    assert err.details["index"] == 0


def test_rotation_commitment_mismatch_rejected(golden, kernel):
    install(golden, kernel)
    err = expect_reject(golden, kernel, "rotation_commitment_mismatch")
    # declared vs provided commitments both surfaced for replay
    assert "declared" in err.details and "provided" in err.details
    assert err.details["declared"] != err.details["provided"]


def test_rotation_without_committee_object_rejected(golden, kernel):
    install(golden, kernel)
    rot = single(golden, "committee_rotation_authorized")
    header = Header.from_dict(rot["header"])
    cert = Certificate.from_dict(rot["certificate"])
    before = tip_tuple(kernel)
    # header announces a commitment, but caller sends no committee object
    with pytest.raises(LightClientError) as ei:
        kernel.apply_header(header, cert, None)
    assert ei.value.code is Code.ROTATION_MISSING_COMMITTEE
    assert ei.value.category is Category.INPUT
    assert tip_tuple(kernel) == before


# --------------------------------------------------------------------------- #
# Branch / equivocation / parent rules
# --------------------------------------------------------------------------- #
def test_unknown_parent_untrusted_branch(golden, kernel):
    install(golden, kernel)
    err = expect_reject(golden, kernel, "untrusted_branch_unknown_parent")
    assert err.details["tip_root"] == golden["checkpoint"]["root"]
    assert err.details["parent_root"] != err.details["tip_root"]


def test_conflicting_header_at_same_round(golden, kernel):
    install(golden, kernel)
    # establish round 101 first
    chain = single(golden, "legal_continuous_3")
    kernel.apply_header(
        Header.from_dict(chain["items"][0]["header"]),
        Certificate.from_dict(chain["items"][0]["certificate"]),
    )
    err = expect_reject(golden, kernel, "conflict_same_round_equivocation")
    assert err.category is Category.STATE_CONFLICT
    # the legitimate tip must still be the first round-101 header
    assert (
        kernel.store.get_tip().tip_header_root.hex()
        == chain["items"][0]["root"][2:]
    )


def test_stale_round_rejected(golden, kernel):
    install(golden, kernel)
    expect_reject(golden, kernel, "stale_round")


def test_cert_bind_mismatch_rejected(golden, kernel):
    install(golden, kernel)
    err = expect_reject(golden, kernel, "cert_bound_to_other_header")
    assert err.category is Category.INPUT
    assert err.details["cert_root"] != err.details["header_root"]


def test_bad_signature_rejected(golden, kernel):
    install(golden, kernel)
    expect_reject(golden, kernel, "bad_signature")


def test_conflicting_certificate_cannot_move_root(golden, kernel):
    """A valid-looking certificate on a header that does NOT extend the tip
    must never move the root. This is the core 'no root update from untrusted
    branch' guarantee, asserted directly."""
    install(golden, kernel)
    vec = single(golden, "untrusted_branch_unknown_parent")
    # Even if its weight is fine, branch rejection wins before crypto.
    err = expect_reject(golden, kernel, "untrusted_branch_unknown_parent")
    assert err.code is Code.UNTRUSTED_BRANCH
