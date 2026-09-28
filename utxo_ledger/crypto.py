"""Signature creation/verification boundary.

Only the mature ``cryptography`` library is used for Ed25519; the ledger itself
never implements primitive arithmetic. Encoding of signed messages lives in
:mod:`utxo_ledger.encoding` (``tx_sighash``) -- this module only signs/verifies
fixed byte strings and translates library exceptions into the ledger error
contract (``COMPUTATION`` for library failures, ``STATE`` for a signature that
is cryptographically well-formed but does not validate).
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from .errors import ErrorCode, LedgerError
from .protocol import PUBLIC_KEY_LEN, SIGNATURE_LEN


class KeyPair:
    """Synthetic-fixture convenience wrapper (test asset, local only)."""

    def __init__(self, private_key: ed25519.Ed25519PrivateKey) -> None:
        self._sk = private_key
        self.public_bytes: bytes = private_key.public_key().public_bytes_raw()

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(ed25519.Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> "KeyPair":
        # Ed25519 seeds are exactly 32 bytes; fixtures derive deterministic ones.
        if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
            raise LedgerError(
                ErrorCode.BAD_PUBLIC_KEY,
                "Ed25519 seed must be exactly 32 bytes",
            )
        return cls(ed25519.Ed25519PrivateKey.from_private_bytes(bytes(seed)))

    def sign(self, message: bytes) -> bytes:
        try:
            return self._sk.sign(message)
        except Exception as exc:  # library defect / bad input type
            raise LedgerError(
                ErrorCode.CRYPTO_FAILURE, f"signing failed: {exc!r}"
            ) from exc

    def public_key_hex(self) -> str:
        return self.public_bytes.hex()


def _load_public_key(pubkey: bytes) -> ed25519.Ed25519PublicKey:
    if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != PUBLIC_KEY_LEN:
        raise LedgerError(
            ErrorCode.BAD_PUBLIC_KEY,
            f"public key must be {PUBLIC_KEY_LEN} bytes, got "
            f"{len(pubkey) if isinstance(pubkey, (bytes, bytearray)) else 'non-bytes'}",
        )
    try:
        return ed25519.Ed25519PublicKey.from_public_bytes(bytes(pubkey))
    except Exception as exc:
        # The library rejects structurally invalid point encodings here.
        raise LedgerError(
            ErrorCode.BAD_PUBLIC_KEY, f"invalid Ed25519 public key: {exc!r}"
        ) from exc


def verify_signature(pubkey: bytes, message: bytes, signature: bytes) -> None:
    """Verify one Ed25519 signature.

    Returns ``None`` on success. Raises:

    * ``LedgerError(BAD_PUBLIC_KEY/BAD_SIGNATURE)`` -- INPUT, malformed material.
    * ``LedgerError(SIG_TAMPERED)`` -- STATE, well-formed signature but invalid
      for ``(pubkey, message)`` (covers message tampering and wrong-key signing).
    * ``LedgerError(CRYPTO_FAILURE)`` -- COMPUTATION, unexpected library error.
    """
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != SIGNATURE_LEN:
        raise LedgerError(
            ErrorCode.BAD_SIGNATURE,
            f"signature must be {SIGNATURE_LEN} bytes, got "
            f"{len(signature) if isinstance(signature, (bytes, bytearray)) else 'non-bytes'}",
        )
    try:
        key = _load_public_key(pubkey)
        key.verify(bytes(signature), bytes(message))
    except InvalidSignature:
        raise LedgerError(
            ErrorCode.SIG_TAMPERED,
            "signature does not verify against the referenced public key",
        )
    except LedgerError:
        raise
    except Exception as exc:  # pragma: no cover - defensive library boundary
        raise LedgerError(
            ErrorCode.CRYPTO_FAILURE, f"verification backend failed: {exc!r}"
        ) from exc


def sign_message(seed_or_keypair: "KeyPair | bytes", message: bytes) -> bytes:
    """Fixture helper: sign with a KeyPair or a 32-byte seed."""
    kp = (
        seed_or_keypair
        if isinstance(seed_or_keypair, KeyPair)
        else KeyPair.from_seed(seed_or_keypair)
    )
    return kp.sign(message)
