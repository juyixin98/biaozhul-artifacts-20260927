"""Deterministic v1 wire encoding and decoding (encoding boundary).

Every structure that crosses a trust boundary uses a length-prefixed,
tagged, big-endian encoding. The tags provide domain separation (a header
byte string can never be re-parsed as a committee, etc.). Decoding is
strict: trailing bytes and truncated input are INPUT_MALFORMED, and all
collections are bounded by the caller-supplied limits.

Digest convention:
    header_digest    = SHA256(encode_header(header))
    committee_id     = SHA256(encode_committee(committee))
Signature messages (see crypto.py) use fixed domain prefixes.
"""

from __future__ import annotations

import hashlib

from .errors import InputMalformed
from .types import (
    Certificate,
    Checkpoint,
    CheckpointEnvelope,
    Committee,
    Header,
    Member,
    Vote,
)

# Domain-separation tags.
_TAG_MEMBER = b"MBR1"
_TAG_COMMITTEE = b"COM1"
_TAG_HEADER = b"HDR1"
_TAG_VOTE = b"VOTE1"
_TAG_CERTIFICATE = b"CERT1"
_TAG_CHECKPOINT = b"CHKP1"
_TAG_ENVELOPE = b"ENV1"

# Signed-message domain prefixes (never equal to any structure encoding tag).
CERT_SIGNING_PREFIX = b"LCv1|certificate|v1\n"
CHECKPOINT_SIGNING_PREFIX = b"LCv1|checkpoint|v1\n"

# Generic safety ceiling even when the caller passes no tighter limit.
_HARD_LIMIT = 1 << 20  # 1 MiB


class _Reader:
    def __init__(self, data: bytes, what: str) -> None:
        self._data = data
        self._pos = 0
        self._what = what

    def _fail(self, msg: str) -> InputMalformed:
        return InputMalformed(f"malformed {self._what}: {msg}")

    def tag(self, expected: bytes) -> None:
        if self._data[self._pos : self._pos + len(expected)] != expected:
            raise self._fail("bad type tag")
        self._pos += len(expected)

    def u8(self, name: str) -> int:
        if self._pos + 1 > len(self._data):
            raise self._fail(f"truncated u8 {name}")
        v = self._data[self._pos]
        self._pos += 1
        return v

    def u64(self, name: str) -> int:
        if self._pos + 8 > len(self._data):
            raise self._fail(f"truncated u64 {name}")
        v = int.from_bytes(self._data[self._pos : self._pos + 8], "big")
        self._pos += 8
        return v

    def u32(self, name: str) -> int:
        if self._pos + 4 > len(self._data):
            raise self._fail(f"truncated u32 {name}")
        v = int.from_bytes(self._data[self._pos : self._pos + 4], "big")
        self._pos += 4
        return v

    def fixed(self, n: int, name: str) -> bytes:
        if self._pos + n > len(self._data):
            raise self._fail(f"truncated fixed {name}")
        v = self._data[self._pos : self._pos + n]
        self._pos += n
        return v

    def var_bytes(self, max_len: int, name: str) -> bytes:
        n = self.u32(name + ".len")
        if n > max_len:
            raise self._fail(f"{name} too long ({n} > {max_len})")
        return self.fixed(n, name)

    def string(self, max_len: int, name: str) -> str:
        raw = self.var_bytes(max_len, name)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise self._fail(f"{name} not utf-8") from None

    def eof(self) -> None:
        if self._pos != len(self._data):
            raise self._fail(f"trailing bytes ({len(self._data) - self._pos})")


def _u64(v: int) -> bytes:
    return int(v).to_bytes(8, "big")


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "big")


def _var(b: bytes) -> bytes:
    return _u32(len(b)) + b


def _list_limited(
    count: int, limit: int, what: str, *, resource_error: bool = False
) -> None:
    if count > limit:
        if resource_error:
            from .errors import ResourceLimit

            raise ResourceLimit(
                f"malformed {what}: too many entries ({count} > {limit})",
                {"count": count, "limit": limit},
            )
        raise InputMalformed(f"malformed {what}: too many entries ({count} > {limit})")


# ---------------------------------------------------------------- members

def encode_member(m: Member) -> bytes:
    return b"".join(
        [
            _TAG_MEMBER,
            _var(m.public_key),
            _u64(m.weight),
        ]
    )


def decode_member(data: bytes) -> Member:
    r = _Reader(data, "member")
    r.tag(_TAG_MEMBER)
    pub = r.var_bytes(32, "public_key")
    if len(pub) != 32:
        raise r._fail("public_key must be 32 bytes")
    weight = r.u64("weight")
    r.eof()
    try:
        return Member(public_key=pub, weight=weight)
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed member: {exc}") from None


# -------------------------------------------------------------- committee

def encode_committee(c: Committee) -> bytes:
    out = [_TAG_COMMITTEE, _u64(c.epoch), _u32(len(c.members))]
    out.extend(encode_member(m) for m in c.members)
    out.append(_u64(c.quorum_weight))
    return b"".join(out)


def decode_committee(data: bytes, *, max_members: int = _HARD_LIMIT) -> Committee:
    r = _Reader(data, "committee")
    r.tag(_TAG_COMMITTEE)
    epoch = r.u64("epoch")
    count = r.u32("members.len")
    _list_limited(count, max_members, "committee", resource_error=True)
    members: list[Member] = []
    for _ in range(count):
        # Each member is independently tagged/framed; decode via a sub-slice
        # is unnecessary because _Reader consumes deterministically.
        members.append(_read_member(r))
    quorum = r.u64("quorum_weight")
    r.eof()
    try:
        return Committee(epoch=epoch, members=tuple(members), quorum_weight=quorum)
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed committee: {exc}") from None


def _read_member(r: _Reader) -> Member:
    r.tag(_TAG_MEMBER)
    pub = r.var_bytes(32, "public_key")
    if len(pub) != 32:
        raise r._fail("public_key must be 32 bytes")
    weight = r.u64("weight")
    try:
        return Member(public_key=pub, weight=weight)
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed member: {exc}") from None


# ----------------------------------------------------------------- header

def encode_header(h: Header) -> bytes:
    out = [
        _TAG_HEADER,
        _var(h.chain_id.encode("utf-8")),
        _u64(h.height),
        _u64(h.round),
        _u64(h.epoch),
        _u64(h.timestamp),
        h.parent_digest,
        h.payload_root,
    ]
    if h.next_committee is None:
        out.append(b"\x00")
    else:
        out.append(b"\x01")
        out.append(encode_committee(h.next_committee))
    return b"".join(out)


def decode_header(
    data: bytes, *, max_committee_members: int = _HARD_LIMIT
) -> Header:
    r = _Reader(data, "header")
    r.tag(_TAG_HEADER)
    chain_id = r.string(256, "chain_id")
    height = r.u64("height")
    round_ = r.u64("round")
    epoch = r.u64("epoch")
    timestamp = r.u64("timestamp")
    parent = r.fixed(32, "parent_digest")
    payload = r.fixed(32, "payload_root")
    flag = r.u8("next_committee.flag")
    if flag not in (0, 1):
        raise r._fail("next_committee flag must be 0 or 1")
    next_committee: Committee | None = None
    if flag == 1:
        # Sub-committee carries its own tag; read it via its reader by
        # capturing the exact remaining prefix is awkward, so re-use the
        # shared primitives directly.
        r.tag(_TAG_COMMITTEE)
        nc_epoch = r.u64("next_committee.epoch")
        count = r.u32("next_committee.members.len")
        _list_limited(count, max_committee_members, "next_committee", resource_error=True)
        members = tuple(_read_member(r) for _ in range(count))
        nc_quorum = r.u64("next_committee.quorum_weight")
        try:
            next_committee = Committee(
                epoch=nc_epoch, members=members, quorum_weight=nc_quorum
            )
        except (TypeError, ValueError) as exc:
            raise InputMalformed(f"malformed next_committee: {exc}") from None
    r.eof()
    try:
        return Header(
            chain_id=chain_id,
            height=height,
            round=round_,
            epoch=epoch,
            timestamp=timestamp,
            parent_digest=parent,
            payload_root=payload,
            next_committee=next_committee,
        )
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed header: {exc}") from None


# ------------------------------------------------------------ certificate

def encode_vote(v: Vote) -> bytes:
    return b"".join([_TAG_VOTE, _var(v.signer), _var(v.signature)])


def _read_vote(r: _Reader) -> Vote:
    r.tag(_TAG_VOTE)
    signer = r.var_bytes(32, "signer")
    if len(signer) != 32:
        raise r._fail("vote.signer must be 32 bytes")
    sig = r.var_bytes(64, "signature")
    if len(sig) != 64:
        raise r._fail("vote.signature must be 64 bytes")
    return Vote(signer=signer, signature=sig)


def encode_certificate(c: Certificate) -> bytes:
    out = [_TAG_CERTIFICATE, c.header_digest, _u32(len(c.votes))]
    out.extend(encode_vote(v) for v in c.votes)
    return b"".join(out)


def decode_certificate(
    data: bytes, *, max_votes: int = _HARD_LIMIT
) -> Certificate:
    r = _Reader(data, "certificate")
    r.tag(_TAG_CERTIFICATE)
    digest = r.fixed(32, "header_digest")
    count = r.u32("votes.len")
    _list_limited(count, max_votes, "certificate", resource_error=True)
    votes = tuple(_read_vote(r) for _ in range(count))
    r.eof()
    try:
        return Certificate(header_digest=digest, votes=votes)
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed certificate: {exc}") from None


# ------------------------------------------------------------- checkpoint

def encode_checkpoint(c: Checkpoint) -> bytes:
    return b"".join(
        [
            _TAG_CHECKPOINT,
            _var(c.chain_id.encode("utf-8")),
            encode_header(c.header),
            encode_committee(c.committee),
            _u64(c.trust_period_seconds),
        ]
    )


def decode_checkpoint(
    data: bytes, *, max_committee_members: int = _HARD_LIMIT
) -> Checkpoint:
    r = _Reader(data, "checkpoint")
    r.tag(_TAG_CHECKPOINT)
    chain_id = r.string(256, "chain_id")
    r.tag(_TAG_HEADER)
    # Re-parse header from a captured sub-slice for clarity: find by reading
    # primitives through the shared reader is not possible across nested
    # tags, so decode nested structures from sub-readers via boundaries.
    # Here we simply read header fields directly (same primitives).
    h_chain = r.string(256, "header.chain_id")
    height = r.u64("height")
    round_ = r.u64("round")
    epoch = r.u64("epoch")
    ts = r.u64("timestamp")
    parent = r.fixed(32, "parent_digest")
    payload = r.fixed(32, "payload_root")
    flag = r.u8("next_committee.flag")
    if flag not in (0, 1):
        raise r._fail("next_committee flag must be 0 or 1")
    next_committee = None
    if flag == 1:
        r.tag(_TAG_COMMITTEE)
        nc_epoch = r.u64("next_committee.epoch")
        count = r.u32("next_committee.members.len")
        _list_limited(count, max_committee_members, "next_committee", resource_error=True)
        members = tuple(_read_member(r) for _ in range(count))
        nc_quorum = r.u64("next_committee.quorum_weight")
        try:
            next_committee = Committee(
                epoch=nc_epoch, members=members, quorum_weight=nc_quorum
            )
        except (TypeError, ValueError) as exc:
            raise InputMalformed(f"malformed next_committee: {exc}") from None
    try:
        header = Header(
            chain_id=h_chain,
            height=height,
            round=round_,
            epoch=epoch,
            timestamp=ts,
            parent_digest=parent,
            payload_root=payload,
            next_committee=next_committee,
        )
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed checkpoint.header: {exc}") from None

    # Nested committee.
    r.tag(_TAG_COMMITTEE)
    c_epoch = r.u64("committee.epoch")
    count = r.u32("committee.members.len")
    _list_limited(count, max_committee_members, "checkpoint.committee", resource_error=True)
    members = tuple(_read_member(r) for _ in range(count))
    quorum = r.u64("committee.quorum_weight")
    try:
        committee = Committee(epoch=c_epoch, members=members, quorum_weight=quorum)
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed checkpoint.committee: {exc}") from None
    trust = r.u64("trust_period_seconds")
    r.eof()
    try:
        return Checkpoint(
            chain_id=chain_id,
            header=header,
            committee=committee,
            trust_period_seconds=trust,
        )
    except (TypeError, ValueError) as exc:
        raise InputMalformed(f"malformed checkpoint: {exc}") from None


def encode_envelope(env: CheckpointEnvelope) -> bytes:
    return b"".join(
        [
            _TAG_ENVELOPE,
            _var(encode_checkpoint(env.checkpoint)),
            _var(env.signature),
        ]
    )


def decode_envelope(
    data: bytes, *, max_committee_members: int = _HARD_LIMIT
) -> CheckpointEnvelope:
    r = _Reader(data, "checkpoint-envelope")
    r.tag(_TAG_ENVELOPE)
    cp_bytes = r.var_bytes(_HARD_LIMIT, "checkpoint")
    sig = r.var_bytes(64, "signature")
    if len(sig) != 64:
        raise r._fail("checkpoint signature must be 64 bytes")
    r.eof()
    checkpoint = decode_checkpoint(
        cp_bytes, max_committee_members=max_committee_members
    )
    return CheckpointEnvelope(checkpoint=checkpoint, signature=sig)


# ---------------------------------------------------------------- digests

def header_digest(h: Header) -> bytes:
    return hashlib.sha256(encode_header(h)).digest()


def committee_id(c: Committee) -> bytes:
    return hashlib.sha256(encode_committee(c)).digest()


def certificate_message(h: Header) -> bytes:
    """Exactly the bytes each committee member signs for a header."""
    return CERT_SIGNING_PREFIX + encode_header(h)


def checkpoint_signing_message(c: Checkpoint) -> bytes:
    """Exactly the bytes the trusted checkpoint key signs."""
    return CHECKPOINT_SIGNING_PREFIX + encode_checkpoint(c)


# --------------------------------------------------------------- json-ish

def hex_(b: bytes) -> str:
    return b.hex()


def unhex(s: str, what: str, length: int | None = None) -> bytes:
    try:
        b = bytes.fromhex(s)
    except (ValueError, TypeError):
        raise InputMalformed(f"{what} is not hex") from None
    if length is not None and len(b) != length:
        raise InputMalformed(f"{what} must be {length} bytes, got {len(b)}")
    return b
