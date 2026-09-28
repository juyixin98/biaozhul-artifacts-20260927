"""Mature cryptographic primitives.

* Keccak-256 (pre-FIPS padding, as Ethereum uses) via PyCryptodome.
* secp256k1 sign / public-key recovery / address derivation via eth-keys
  (backed by coincurve/libsecp256k1).

The hand-written ABI codec never implements a hash or curve itself; it calls
these audited primitives.
"""
from __future__ import annotations

from dataclasses import dataclass

from Crypto.Hash import keccak as _keccak  # type: ignore
import eth_keys  # type: ignore


def keccak256(data: bytes) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("keccak256 expects bytes")
    h = _keccak.new(digest_bits=256)
    h.update(bytes(data))
    return h.digest()


@dataclass(frozen=True)
class Signature:
    r: int
    s: int
    v: int  # recovery id, 0/1

    def to_bytes(self) -> bytes:
        return self.r.to_bytes(32, "big") + self.s.to_bytes(32, "big") + bytes([self.v])

    @staticmethod
    def from_bytes(raw: bytes) -> "Signature":
        if len(raw) != 65:
            raise ValueError("signature must be 65 bytes (r||s||v)")
        r = int.from_bytes(raw[:32], "big")
        s = int.from_bytes(raw[32:64], "big")
        v = raw[64]
        if v not in (0, 1):
            raise ValueError("recovery id must be 0 or 1")
        return Signature(r=r, s=s, v=v)


def generate_privkey(seed: int) -> bytes:
    """Deterministically derive a 32-byte private key from an integer seed.

    Uses Keccak over a labelled seed so fixtures are reproducible without any
    external account.
    """
    digest = keccak256(b"abibackend:key:v1:" + seed.to_bytes(32, "big", signed=False))
    return digest


def privkey_address(privkey: bytes) -> bytes:
    """Return the 20-byte Ethereum address for a private key."""
    key = eth_keys.keys.PrivateKey(bytes(privkey))
    pub = key.public_key.to_bytes()  # 64-byte uncompressed X||Y
    return keccak256(pub)[-20:]


def sign_digest(privkey: bytes, digest: bytes) -> Signature:
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    key = eth_keys.keys.PrivateKey(bytes(privkey))
    sig = key.sign_msg_hash(bytes(digest))
    # eth_keys exposes v as 0/1 in Signature.v for msg_hash signing.
    return Signature(r=sig.r, s=sig.s, v=sig.v)


def recover_address(digest: bytes, signature: Signature) -> bytes:
    """Recover the 20-byte signer address from a digest and signature."""
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    sig = eth_keys.keys.Signature(
        signature.r.to_bytes(32, "big") + signature.s.to_bytes(32, "big") + bytes([signature.v])
    )
    pub = sig.recover_public_key_from_msg_hash(bytes(digest))
    return keccak256(pub.to_bytes())[-20:]
