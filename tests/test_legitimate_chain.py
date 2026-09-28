"""Continuous legitimate chain + agreement with independent golden vectors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

from lightclient import codec
from lightclient.kernel import LightClientKernel
from lightclient.replay import ReplayEngine, ReplayItem
from lightclient.store import Store
from lightclient.config import LightClientConfig

GOLDEN = json.loads((Path(__file__).parent / "golden_vectors.json").read_text())


def _golden_kernel(store_path=":memory:"):
    """Bootstrap a kernel strictly from the committed golden envelope bytes."""
    env = codec.decode_envelope(bytes.fromhex(GOLDEN["checkpoint_envelope_hex"]))
    cfg = LightClientConfig(
        chain_id=GOLDEN["chain_id"],
        trust_period_seconds=GOLDEN["trust_period_seconds"],
        quorum_weight=GOLDEN["quorum_weight"],
    )
    store = Store(store_path)
    k = LightClientKernel(store, cfg, bytes.fromhex(GOLDEN["checkpoint_public_hex"]))
    tip = k.bootstrap(env)
    return k, tip


def test_continuous_legitimate_headers_all_accepted(bootstrapped, request, log_case):
    kernel, builder = bootstrapped
    c1 = builder.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])
    blocks = []
    blocks.append(builder.add_block(signer_labels=["c0-a", "c0-b"]))
    blocks.append(builder.add_block(signer_labels=["c0-b", "c0-c"]))
    announce = builder.add_block(
        signer_labels=["c0-a", "c0-c"], next_committee=c1.committee
    )
    blocks.append(announce)
    blocks.append(builder.add_block(signer_labels=["c1-a", "c1-c"], epoch=1))
    blocks.append(builder.add_block(signer_labels=["c1-a", "c1-b"], epoch=1))

    for blk in blocks:
        result = kernel.apply_header(blk.header, blk.certificate)
        log_case(
            "apply",
            height=blk.header.height,
            epoch=blk.header.epoch,
            decision=result.decision,
            signed_weight=result.certificate.signed_weight,
        )
        assert result.decision == "accepted"
        assert result.tip.height == blk.header.height
        assert result.tip.epoch == blk.header.epoch

    # committee change persisted and indexed
    c1_id = codec.committee_id(c1.committee).hex()
    assert kernel.store.has_committee(bytes.fromhex(c1_id))
    assert kernel.tip().epoch == 1


def test_golden_digests_match_independent_sha256(bootstrapped):
    """Each golden header must be accepted AND match the independent digest."""
    kernel, builder = bootstrapped
    for vec in GOLDEN["headers"]:
        header = codec.decode_header(bytes.fromhex(vec["wire_hex"]))
        cert = codec.decode_certificate(bytes.fromhex(vec["cert_hex"]))
        # independent recomputation: plain hashlib over the same wire bytes
        assert hashlib.sha256(bytes.fromhex(vec["wire_hex"])).hexdigest() == vec[
            "sha256_independent_hex"
        ]
        assert vec["digest_hex"] == vec["sha256_independent_hex"]
        result = kernel.apply_header(header, cert)
        assert result.decision == "accepted"
        assert result.digest.hex() == vec["digest_hex"]
        assert result.certificate.signed_weight == vec["signed_weight"]
    assert kernel.tip().height == 4
    assert kernel.tip().epoch == 1


def test_golden_signature_verifies_with_library_directly():
    for chk in GOLDEN["signature_checks"]:
        pub = bytes.fromhex(chk["pub_hex"])
        sig = bytes.fromhex(chk["signature_hex"])
        # Reconstruct the exact signed message independently: prefix + header.
        header_wire = bytes.fromhex(
            next(h["wire_hex"] for h in GOLDEN["headers"] if h["height"] == chk["height"])
        )
        msg = codec.CERT_SIGNING_PREFIX + header_wire
        ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        assert chk["verifies_independently"] is True


def test_golden_quorum_arithmetic_independent():
    for case in GOLDEN["quorum_cases"]:
        assert sum(case["weights"]) == case["sum"]
        assert (sum(case["weights"]) >= case["quorum"]) == case["reaches_quorum"]


def test_persisted_state_survives_reopen(tmp_path):
    k, tip = _golden_kernel(str(tmp_path / "lc.db"))
    items = []
    for vec in GOLDEN["headers"][:3]:
        items.append(
            ReplayItem(
                codec.decode_header(bytes.fromhex(vec["wire_hex"])),
                codec.decode_certificate(bytes.fromhex(vec["cert_hex"])),
            )
        )
    report = ReplayEngine(k).replay(items)
    assert report.ok
    tip_digest = k.tip().digest
    k.store.close()

    # reopen — no re-bootstrap; trusted tip must still be there
    store2 = Store(str(tmp_path / "lc.db"))
    k2 = LightClientKernel(
        store2,
        LightClientConfig(
            chain_id=GOLDEN["chain_id"],
            trust_period_seconds=GOLDEN["trust_period_seconds"],
            quorum_weight=GOLDEN["quorum_weight"],
        ),
        bytes.fromhex(GOLDEN["checkpoint_public_hex"]),
    )
    assert k2.is_initialized()
    assert k2.tip().digest == tip_digest
    assert k2.tip().height == 3
    # and the chain continues to extend
    vec = GOLDEN["headers"][3]
    result = k2.apply_header(
        codec.decode_header(bytes.fromhex(vec["wire_hex"])),
        codec.decode_certificate(bytes.fromhex(vec["cert_hex"])),
    )
    assert result.decision == "accepted"
    assert result.tip.height == 4
    store2.close()
