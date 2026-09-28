"""Standalone evidence verification (see docs/protocol.md).

The checker takes a single evidence packet and returns a structured verdict:

    {"valid": bool, "reason": str, "checks": [...], "validator": ...}

Reasons (``valid=false``):
    invalid_format      packet structure / field encoding wrong
    bad_domain          chain ids disagree inside a vote or packet
    bad_signature       an Ed25519 signature does not verify
    not_member          claimed validator absent from its target snapshot
    bad_snapshot_root   snapshot root does not match its member list
    rule_mismatch       the two votes do not satisfy the claimed offense type
    bad_evidence_id     recomputed id differs from the packet's id
    unknown_type        offense type/version unsupported
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

# ---- constants copied from the protocol specification, not imported --------
DOMAIN_VOTE = b"ffg-slash v1 vote" + b"\x00" * 15
DOMAIN_SNAPSHOT = b"ffg-slash v1 snapshot" + b"\x00" * 11
SUPPORTED_VERSIONS = {1}
KNOWN_TYPES = {"double_vote", "surround_vote"}


class CheckerError(ValueError):
    """Structural failure during packet reading (maps to invalid_format)."""


@dataclass
class Verdict:
    valid: bool
    reason: str
    validator: str | None = None
    evidence_id: str | None = None
    offense: str | None = None
    checks: list[str] = field(default_factory=list)
    detail: str | None = None

    def as_dict(self) -> dict:
        out = {
            "valid": self.valid,
            "reason": self.reason,
            "validator": self.validator,
            "evidence_id": self.evidence_id,
            "offense": self.offense,
            "checks": self.checks,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


def _u64(value, name):
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckerError(f"{name} must be integer")
    if not 0 <= value <= 2**64 - 1:
        raise CheckerError(f"{name} out of uint64 range")
    return value


def _b(value, n, name):
    if not isinstance(value, str):
        raise CheckerError(f"{name} must be hex string")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise CheckerError(f"{name} bad hex") from exc
    if len(raw) != n:
        raise CheckerError(f"{name} must be {n} bytes")
    return raw


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


# ---- independent re-implementation of the encoding --------------------------

def _body(src_e, src_r, tgt_e, tgt_r):
    return (src_e.to_bytes(8, "big") + src_r + tgt_e.to_bytes(8, "big") + tgt_r)


def _signing_preimage(chain_id, pub, src_e, src_r, tgt_e, tgt_r):
    return (DOMAIN_VOTE + chain_id.to_bytes(8, "big") + pub
            + _body(src_e, src_r, tgt_e, tgt_r))


def _snapshot_root(chain_id, epoch, members):
    parts = [DOMAIN_SNAPSHOT, chain_id.to_bytes(8, "big"),
             epoch.to_bytes(8, "big"), len(members).to_bytes(8, "big")]
    for pub, weight in sorted(members, key=lambda m: m[0]):
        parts.append(pub)
        parts.append(weight.to_bytes(8, "big"))
    return _sha(b"".join(parts))


def _ed25519_verify(pub: bytes, sig: bytes, message: bytes) -> bool:
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---- packet reading ----------------------------------------------------------

def _read_vote(obj):
    if not isinstance(obj, dict):
        raise CheckerError("vote must be object")
    chain_id = _u64(obj.get("chain_id"), "vote.chain_id")
    pub = _b(obj.get("validator_pubkey"), 32, "vote.validator_pubkey")
    se = _u64(obj.get("source_epoch"), "vote.source_epoch")
    sr = _b(obj.get("source_root"), 32, "vote.source_root")
    te = _u64(obj.get("target_epoch"), "vote.target_epoch")
    tr = _b(obj.get("target_root"), 32, "vote.target_root")
    sig = _b(obj.get("signature"), 64, "vote.signature")
    if se >= te:
        raise CheckerError("vote source_epoch must be < target_epoch")
    return chain_id, pub, se, sr, te, tr, sig


def _read_snapshot(obj):
    if not isinstance(obj, dict):
        raise CheckerError("snapshot must be object")
    chain_id = _u64(obj.get("chain_id"), "snapshot.chain_id")
    epoch = _u64(obj.get("epoch"), "snapshot.epoch")
    root = _b(obj.get("root"), 32, "snapshot.root")
    members_raw = obj.get("members")
    if not isinstance(members_raw, list):
        raise CheckerError("snapshot.members must be list")
    members = []
    for m in members_raw:
        pk = _b(m.get("pubkey"), 32, "member.pubkey")
        w = _u64(m.get("weight"), "member.weight")
        if w <= 0:
            raise CheckerError("member weight must be positive")
        members.append((pk, w))
    if len({pk for pk, _ in members}) != len(members):
        raise CheckerError("duplicate member pubkey in snapshot")
    return chain_id, epoch, root, members


def _canonical_core(packet: dict) -> bytes:
    core = {k: packet[k] for k in (
        "version", "type", "chain_id", "validator_pubkey",
        "vote_1", "vote_2", "vote_1_snapshot", "vote_2_snapshot")}
    return json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


# ---- public API ---------------------------------------------------------------

def review(packet: dict | str | bytes) -> Verdict:
    try:
        if isinstance(packet, (str, bytes, bytearray)):
            try:
                packet = json.loads(packet)
            except ValueError as exc:
                return Verdict(False, "invalid_format",
                               detail=f"packet is not valid JSON: {exc}")
        if not isinstance(packet, dict):
            return Verdict(False, "invalid_format",
                           detail="packet must be a JSON object")

        version = packet.get("version")
        if version not in SUPPORTED_VERSIONS:
            return Verdict(False, "unknown_type", detail=f"unsupported version {version!r}")
        offense = packet.get("type")
        if offense not in KNOWN_TYPES:
            return Verdict(False, "unknown_type", detail=f"unsupported type {offense!r}")

        chain_id = _u64(packet.get("chain_id"), "chain_id")
        validator = _b(packet.get("validator_pubkey"), 32, "validator_pubkey")
        evidence_id = packet.get("evidence_id")
        if not isinstance(evidence_id, str):
            raise CheckerError("evidence_id missing")

        v1 = _read_vote(packet["vote_1"])
        v2 = _read_vote(packet["vote_2"])
        s1 = _read_snapshot(packet["vote_1_snapshot"])
        s2 = _read_snapshot(packet["vote_2_snapshot"])
        checks = ["format: packet structure valid"]

        def fail(reason, extra_checks=None, detail=None):
            return Verdict(False, reason, validator.hex(), evidence_id, offense,
                           checks + (extra_checks or []), detail)

        # domain consistency: packet chain id binds every embedded value
        if v1[0] != chain_id or v2[0] != chain_id:
            return fail("bad_domain", ["domain: vote chain id mismatch"])
        if s1[0] != chain_id or s2[0] != chain_id:
            return fail("bad_domain", ["domain: snapshot chain id mismatch"])
        if v1[1] != validator or v2[1] != validator:
            return fail("bad_domain", ["domain: embedded signer != packet validator"])
        checks.append("domain: chain_id and validator binding consistent")

        # cryptographic verification — gate for everything else
        for tag, v in (("vote_1", v1), ("vote_2", v2)):
            cid, pub, se, sr, te, tr, sig = v
            msg = _signing_preimage(cid, pub, se, sr, te, tr)
            if not _ed25519_verify(pub, sig, msg):
                return fail("bad_signature",
                            [f"signature: {tag} failed Ed25519 verification"])
        checks.append("signature: both votes verify under the claimed pubkey")

        # snapshot authenticity first, then membership against each vote's
        # own target-epoch snapshot
        for tag, v, s in (("vote_1", v1, s1), ("vote_2", v2, s2)):
            cid_v, pub, se, sr, te, tr, sig = v
            scid, sepoch, sroot, members = s
            if te != sepoch:
                return fail("invalid_format",
                            [f"{tag}: snapshot epoch != vote target epoch"])
            if _snapshot_root(scid, sepoch, members) != sroot:
                return fail("bad_snapshot_root",
                            [f"snapshot_root: epoch {te} does not match members"])
        checks.append("snapshot_root: both roots recompute from member lists")
        for tag, v, s in (("vote_1", v1, s1), ("vote_2", v2, s2)):
            pub = v[1]
            te = v[4]
            members = s[3]
            if dict(members).get(pub, 0) <= 0:
                return fail("not_member",
                            [f"membership: signer absent from epoch {te}"])
        checks.append("membership: signer present in both target snapshots")

        # rule matching against the claimed offense
        (_, _, s1e, s1r, t1e, t1r, _) = v1
        (_, _, s2e, s2r, t2e, t2r, _) = v2
        if offense == "double_vote":
            body1 = _body(s1e, s1r, t1e, t1r)
            body2 = _body(s2e, s2r, t2e, t2r)
            if not (t1e == t2e and _sha(body1) != _sha(body2)):
                return fail("rule_mismatch",
                            ["rule: not (equal target epoch AND differing content)"])
            checks.append("rule: equal target epoch with different message content")
        else:  # surround_vote — either ordering, strictly nested
            nested = (s1e < s2e and t2e < t1e) or (s2e < s1e and t1e < t2e)
            if not nested:
                return fail("rule_mismatch",
                            ["rule: intervals are not strictly nested"])
            checks.append(
                "rule: intervals strictly nested "
                "(outer src < inner src < inner tgt < outer tgt)")

        # evidence id recomputation (tamper detection over the full packet)
        recomputed = _sha(_canonical_core(packet)).hex()
        if recomputed != evidence_id:
            return fail("bad_evidence_id",
                        [f"id: recomputed {recomputed[:12]} != packet {evidence_id[:12]}"])
        checks.append("evidence_id: canonical SHA-256 matches packet")

        return Verdict(True, "valid", validator.hex(), evidence_id, offense,
                       checks + ["verdict: VALID — independently verified offense"])
    except CheckerError as exc:
        return Verdict(False, "invalid_format", detail=str(exc))
    except (KeyError, TypeError) as exc:
        return Verdict(False, "invalid_format", detail=f"missing/malformed field: {exc}")
