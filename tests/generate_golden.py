"""Generate committed golden test vectors.

Run: .venv/bin/python tests/generate_golden.py
Output: tests/golden_vectors.json

The answers in here are produced by the independent fixture builder and then
*independently re-derived* (hashlib SHA-256 over canonical codec bytes,
``cryptography`` Ed25519 verification, plain integer weight sums). The light
client kernel is never imported here, so tests do not grade their own work.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.asymmetric import ed25519

from lightclient import codec
from lightclient.fixtures.builder import (
    ChainBuilder,
    build_certificate,
    build_header,
)

OUT = Path(__file__).resolve().parent / "golden_vectors.json"

CHAIN_ID = "local-test-chain-0001"
GENESIS_TS = 1_000_000
TRUST_PERIOD = 3600
QUORUM = 2


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).digest().hex()


def raw_verify(pub: bytes, sig: bytes, msg: bytes) -> bool:
    """Independent reference check, straight from the crypto library."""
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except Exception:
        return False


def independent_weight(weights: list[int]) -> int:
    total = 0
    for w in weights:
        total += w  # plain loop, not the crypto layer
    return total


def main() -> None:
    b = ChainBuilder(
        chain_id=CHAIN_ID, trust_period_seconds=TRUST_PERIOD, quorum_weight=QUORUM
    )
    gh, env = b.genesis(timestamp=GENESIS_TS)

    c0 = b.genesis_secrets
    c1 = b.add_committee(1, [("c1-a", 1), ("c1-b", 1), ("c1-c", 1)])

    # Three legitimate epoch-0 headers; h3 announces committee c1.
    h1 = b.add_block(signer_labels=["c0-a", "c0-b"])
    h2 = b.add_block(signer_labels=["c0-b", "c0-c"])
    h3 = b.add_block(signer_labels=["c0-a", "c0-c"], next_committee=c1.committee)
    # First epoch-1 header signed by the announced committee.
    h4 = b.add_block(signer_labels=["c1-a", "c1-b"], epoch=1)

    def header_vec(h):
        wire = codec.encode_header(h.header)
        return {
            "height": h.header.height,
            "round": h.header.round,
            "epoch": h.header.epoch,
            "timestamp": h.header.timestamp,
            "wire_hex": wire.hex(),
            "digest_hex": codec.header_digest(h.header).hex(),
            # independently re-derived digest, must match the codec one
            "sha256_independent_hex": sha256_hex(wire),
            "parent_hex": h.header.parent_digest.hex(),
            "signer_labels": h.signer_labels,
            "signed_weight": h.signed_weight,
            "cert_hex": codec.encode_certificate(h.certificate).hex(),
        }

    vectors = {
        "format": "golden/local-header-lightclient/v1",
        "chain_id": CHAIN_ID,
        "genesis_timestamp": GENESIS_TS,
        "trust_period_seconds": TRUST_PERIOD,
        "quorum_weight": QUORUM,
        "checkpoint_public_hex": b.checkpoint_pub.hex(),
        "checkpoint_envelope_hex": codec.encode_envelope(env).hex(),
        "genesis": {
            "height": gh.height,
            "round": gh.round,
            "epoch": gh.epoch,
            "timestamp": gh.timestamp,
            "digest_hex": codec.header_digest(gh).hex(),
            "sha256_independent_hex": sha256_hex(codec.encode_header(gh)),
        },
        "committees": {
            "c0": {
                "epoch": 0,
                "id_hex": codec.committee_id(c0.committee).hex(),
                "members": [
                    {
                        "label": lab,
                        "pub_hex": c0.public_for(lab).hex(),
                        "weight": c0.committee.member_by_key(
                            c0.public_for(lab)
                        ).weight,
                    }
                    for lab in ["c0-a", "c0-b", "c0-c"]
                ],
                "total_weight": c0.committee.total_weight,
                "quorum_weight": c0.committee.quorum_weight,
            },
            "c1": {
                "epoch": 1,
                "id_hex": codec.committee_id(c1.committee).hex(),
                "members": [
                    {
                        "label": lab,
                        "pub_hex": c1.public_for(lab).hex(),
                        "weight": 1,
                    }
                    for lab in ["c1-a", "c1-b", "c1-c"]
                ],
                "total_weight": c1.committee.total_weight,
                "quorum_weight": c1.committee.quorum_weight,
            },
        },
        "headers": [header_vec(x) for x in [h1, h2, h3, h4]],
    }

    # Independent signature spot-checks (message = prefix + header bytes).
    msg_h1 = codec.certificate_message(h1.header)
    vectors["signature_checks"] = [
        {
            "height": 1,
            "signer_label": "c0-a",
            "pub_hex": c0.public_for("c0-a").hex(),
            "signature_hex": h1.certificate.votes[0].signature.hex(),
            "message_sha256_hex": hashlib.sha256(msg_h1).hexdigest(),
            "verifies_independently": raw_verify(
                c0.public_for("c0-a"), h1.certificate.votes[0].signature, msg_h1
            ),
        }
    ]

    # Weighted quorum answers derived without crypto/kernel.
    vectors["quorum_cases"] = [
        {"weights": [1], "sum": independent_weight([1]), "quorum": QUORUM,
         "reaches_quorum": independent_weight([1]) >= QUORUM},
        {"weights": [1, 1], "sum": independent_weight([1, 1]),
         "quorum": QUORUM, "reaches_quorum": True},
        {"weights": [2], "sum": independent_weight([2]), "quorum": QUORUM,
         "reaches_quorum": True},
    ]

    # Trust-period boundary answers (pure arithmetic).
    vectors["trust_boundary"] = {
        "tip_timestamp": GENESIS_TS,
        "at_boundary_ts": GENESIS_TS + TRUST_PERIOD,
        "beyond_boundary_ts": GENESIS_TS + TRUST_PERIOD + 1,
        "boundary_gap": TRUST_PERIOD,
        "boundary_accepted": TRUST_PERIOD <= TRUST_PERIOD,
        "beyond_needs_checkpoint": TRUST_PERIOD + 1 > TRUST_PERIOD,
    }

    # A conflict header: same height as h1, different payload, properly signed
    # by the genesis committee (equivocation must still be refused).
    conflict_header = build_header(
        chain_id=CHAIN_ID,
        height=1,
        round=1,
        epoch=0,
        timestamp=GENESIS_TS + 10,
        parent_digest=codec.header_digest(gh),
        payload=b"\xde\xad\xbe\xef" * 8,
    )
    conflict_cert = build_certificate(
        conflict_header, [c0.seed_for("c0-a"), c0.seed_for("c0-b")]
    )
    vectors["conflict"] = {
        "height": 1,
        "wire_hex": codec.encode_header(conflict_header).hex(),
        "digest_hex": codec.header_digest(conflict_header).hex(),
        "cert_hex": codec.encode_certificate(conflict_cert).hex(),
        "differs_from_h1": codec.header_digest(conflict_header)
        != codec.header_digest(h1.header),
    }

    # Tampered signature vector: flip a signature byte.
    bad_votes = []
    v0 = h1.certificate.votes[0]
    tampered_sig = bytes([v0.signature[0] ^ 0x01]) + v0.signature[1:]
    bad_votes.append({"signer_hex": v0.signer.hex(), "sig_hex": tampered_sig.hex()})
    vectors["tampered_signature"] = {
        "height": 1,
        "header_wire_hex": codec.encode_header(h1.header).hex(),
        "votes": bad_votes,
        "verifies_independently": raw_verify(
            v0.signer, tampered_sig, codec.certificate_message(h1.header)
        ),
    }

    OUT.write_text(json.dumps(vectors, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT}")
    # Self-check the independent assertions.
    for h in [h1, h2, h3, h4]:
        assert h.header and h.certificate
    assert vectors["headers"][0]["digest_hex"] == vectors["headers"][0][
        "sha256_independent_hex"
    ]
    assert vectors["signature_checks"][0]["verifies_independently"] is True
    assert vectors["conflict"]["differs_from_h1"] is True
    assert vectors["tampered_signature"]["verifies_independently"] is False
    print("golden self-checks passed")


if __name__ == "__main__":
    main()
