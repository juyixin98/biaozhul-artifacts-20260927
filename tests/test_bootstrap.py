"""Bootstrap / trusted-root tests.

The root may only be installed once, out-of-band, through a checkpoint
signed by the pinned trusted key. Headers can never re-root the client.
"""

from __future__ import annotations

import pytest

from lightclient.config import LightClientConfig
from lightclient.errors import ErrorCode
from lightclient.fixtures.builder import ChainBuilder
from lightclient.kernel import LightClientKernel
from lightclient.store import Store


def _uninitialized(builder):
    return LightClientKernel(
        Store(":memory:"), LightClientConfig(), builder.checkpoint_pub
    )


def test_valid_checkpoint_bootstraps_once():
    builder = ChainBuilder()
    _g, env = builder.genesis()
    k = _uninitialized(builder)
    assert k.is_initialized() is False
    tip = k.bootstrap(env)
    assert k.is_initialized() is True
    assert tip.height == 0
    assert k.chain_id() == builder.chain_id


def test_second_bootstrap_is_always_rejected_even_with_valid_signature():
    builder = ChainBuilder()
    _g, env = builder.genesis()
    k = _uninitialized(builder)
    k.bootstrap(env)
    before = k.tip().digest
    # A second, independently-signed, perfectly valid checkpoint from the
    # SAME trusted key still cannot move the root of an initialized client.
    _g2, env2 = builder.genesis(timestamp=2_000_000)
    with pytest.raises(Exception) as ei:
        k.bootstrap(env2)
    assert ei.value.code == ErrorCode.ALREADY_INITIALIZED
    assert ei.value.category.value == "state"
    assert k.tip().digest == before


def test_forged_checkpoint_signature_rejected():
    builder = ChainBuilder()
    _g, env = builder.genesis()
    # Pin a different trusted key (attacker does not hold the real one).
    from lightclient.fixtures.builder import build_key

    _seed, attacker_pub = build_key("someone-else")
    k = LightClientKernel(
        Store(":memory:"), LightClientConfig(), attacker_pub
    )
    with pytest.raises(Exception) as ei:
        k.bootstrap(env)
    assert ei.value.code == ErrorCode.CHECKPOINT_SIGNATURE_INVALID
    assert ei.value.category.value == "input"
    assert k.is_initialized() is False


def test_checkpoint_with_tampered_body_rejected():
    builder = ChainBuilder()
    _g, env = builder.genesis()
    # Flip a byte of the signed *body* (the header's payload root) while
    # keeping the original signature. Decoding succeeds but signature
    # verification over the changed canonical bytes must fail.
    cp = env.checkpoint
    tampered_header = cp.header.__class__(
        chain_id=cp.header.chain_id,
        height=cp.header.height,
        round=cp.header.round,
        epoch=cp.header.epoch,
        timestamp=cp.header.timestamp,
        parent_digest=cp.header.parent_digest,
        payload_root=bytes([cp.header.payload_root[0] ^ 0xFF])
        + cp.header.payload_root[1:],
        next_committee=cp.header.next_committee,
    )
    tampered_cp = cp.__class__(
        chain_id=cp.chain_id,
        header=tampered_header,
        committee=cp.committee,
        trust_period_seconds=cp.trust_period_seconds,
    )
    tampered_env = env.__class__(checkpoint=tampered_cp, signature=env.signature)
    assert tampered_env != env
    k = _uninitialized(builder)
    with pytest.raises(Exception) as ei:
        k.bootstrap(tampered_env)
    assert ei.value.code == ErrorCode.CHECKPOINT_SIGNATURE_INVALID
    assert k.is_initialized() is False


def test_checkpoint_for_other_chain_rejected():
    builder = ChainBuilder(chain_id="local-test-chain-0001")
    other = ChainBuilder(chain_id="totally-different-chain")
    _g, env = other.genesis()
    k = _uninitialized(builder)
    with pytest.raises(Exception) as ei:
        k.bootstrap(env)
    # Pinning the same deterministic trusted key means signature may verify;
    # either way the checkpoint must be refused. If signatures happen to
    # verify (identical derived key labels), the chain binding catches it.
    assert ei.value.code in (
        ErrorCode.CHECKPOINT_SIGNATURE_INVALID,
        ErrorCode.CHAIN_MISMATCH,
    )
    assert k.is_initialized() is False


def test_checkpoint_for_other_chain_signed_by_foreign_key_rejected_at_sig():
    # A genuinely foreign trusted keypair (different derivation label) must
    # fail at signature verification before the chain binding is reached.
    from lightclient.fixtures.builder import build_key

    _foreign_seed, foreign_pub = build_key("distinct-foreign-trust-anchor")
    other = ChainBuilder(chain_id="totally-different-chain")
    # re-sign the other chain's checkpoint with its own genesis-derived key
    _g, env = other.genesis()
    k = LightClientKernel(
        Store(":memory:"), LightClientConfig(), foreign_pub
    )
    with pytest.raises(Exception) as ei:
        k.bootstrap(env)
    assert ei.value.code == ErrorCode.CHECKPOINT_SIGNATURE_INVALID
    assert k.is_initialized() is False


def test_header_cannot_update_root_before_initialization():
    builder = ChainBuilder()
    _g, env = builder.genesis()
    k = _uninitialized(builder)
    blk = builder.add_block(signer_labels=["c0-a", "c0-b"])
    with pytest.raises(Exception) as ei:
        k.apply_header(blk.header, blk.certificate)
    assert ei.value.code == ErrorCode.NOT_INITIALIZED
    assert k.is_initialized() is False
