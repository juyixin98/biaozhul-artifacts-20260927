"""Salt generation and the low-entropy policy.

Random salts come from the operating system CSPRNG via :mod:`secrets`.
Salting hides a *high-entropy* value behind a commitment but it does **not**
turn a small value space into a secret: for a boolean or any other low-entropy
field the verifier can simply hash ``salt?`` no -- they do not know the salt.

Important limitation (documented in docs/PROTOCOL.md and surfaced on every
batch): even with a per-field CSPRNG salt, low-entropy fields remain vulnerable
to dictionary/enumeration attack by anyone who obtains the *salt*, and without
a salt they are trivially enumerable by anyone with the commitment. The salt
only has to stay private until disclosure; it cannot protect an intrinsically
guessable value once disclosed against related-context inference. The service
therefore (a) refuses unsalted commitments unless explicitly enabled and
(b) flags declared low-entropy fields with an advisory.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.core.errors import PolicyViolation

MIN_SALT_BYTES = 16
# Types whose value space is intrinsically enumerable. Booleans are always
# flagged; text/decimal fields may be flagged explicitly by the submitter.
LOW_ENTROPY_TYPES = frozenset({"bool"})


@dataclass(frozen=True)
class SaltPolicy:
    digest_name: str = "sha256"
    salt_bytes: int = MIN_SALT_BYTES
    allow_unsalted: bool = False

    def __post_init__(self) -> None:
        if self.salt_bytes < MIN_SALT_BYTES:
            raise PolicyViolation(
                f"salt length {self.salt_bytes} is below minimum "
                f"{MIN_SALT_BYTES}"
            )
        if self.salt_bytes > 64:
            raise PolicyViolation("salt length above 64 is unreasonable")


def random_salt(num_bytes: int) -> bytes:
    """Return ``num_bytes`` of CSPRNG output."""
    if num_bytes < 1:
        raise PolicyViolation("salt length must be >= 1")
    return secrets.token_bytes(num_bytes)


def is_declared_low_entropy(field_type: str, value_space: int | None) -> bool:
    """Decide whether a field must carry an enumeration advisory.

    ``value_space`` is an optional submitter-declared cardinality bound.
    """
    if field_type in LOW_ENTROPY_TYPES:
        return True
    if value_space is not None and value_space <= 2**20:
        return True
    return False
