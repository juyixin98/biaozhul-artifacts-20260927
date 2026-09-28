"""Independent reference oracle.

This module is *not* part of the production package and must never import
``basefee``. It is an independent re-derivation of the EIP-1559 fee rules used
to:

1. supply expected values for the committed synthetic fixtures (generated once by
   ``scripts/build_fixtures.py``), and
2. cross-check the kernel in ``tests/test_oracle_consistency.py`` over random
   inputs.

It deliberately expresses the recurrence differently from the kernel (single
function, dict output, explicit local constants parsed from the same JSON
config treated as *data*). Independence claim: if both implementations agree
over fuzzed inputs AND the kernel matches committed hand-computed vectors, a
shared coding mistake is far less likely than either check alone.

Signing here uses the ``ecdsa`` library directly (a mature external primitive),
exactly mirroring the documented synthetic wire format, without going through
the project's own crypto wrapper.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ecdsa import SigningKey, VerifyingKey, SECP256k1
from ecdsa.util import sigencode_string, sigdecode_string

CONFIG = json.loads(
    Path(__file__).resolve().parents[1].joinpath("config", "protocol.json")
    .read_text(encoding="utf-8")
)
CHAIN_ID = int(CONFIG["chain_id"])
ELASTICITY = int(CONFIG["elasticity_multiplier"])
DENOMINATOR = int(CONFIG["base_fee_max_change_denominator"])
INTRINSIC_GAS = int(CONFIG["intrinsic_tx_gas"])
MIN_BASE_FEE = int(CONFIG["min_base_fee"])
GENESIS_BASE_FEE = int(CONFIG["genesis_base_fee"])
GENESIS_GAS_LIMIT = int(CONFIG["genesis_gas_limit"])
HALF_N = SECP256k1.order // 2


# --------------------------------------------------------------------------- #
# Independent RLP (written separately from src/basefee/encoding/rlp.py)
# --------------------------------------------------------------------------- #
def _rlp_len(n: int, base: int) -> bytes:
    if n < 56:
        return bytes([base + n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([base + 55 + len(body)]) + body


def rlp_encode(item) -> bytes:
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        return _rlp_len(len(item), 0x80) + item
    payload = b"".join(rlp_encode(x) for x in item)
    return _rlp_len(len(payload), 0xC0) + payload


def _scalar(n: int) -> bytes:
    if n < 0:
        raise ValueError("negative")
    return n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""


# --------------------------------------------------------------------------- #
# Independent fee recurrence
# --------------------------------------------------------------------------- #
def oracle_target(gas_limit: int) -> int:
    assert gas_limit > 0 and gas_limit % ELASTICITY == 0
    return gas_limit // ELASTICITY


def oracle_next_base_fee(base_fee: int, gas_used: int, gas_limit: int) -> int:
    """Reference EIP-1559 update with floor arithmetic and +1 wei minimum."""
    if gas_used < 0 or gas_used > gas_limit:
        raise ValueError("gas out of range")
    target = oracle_target(gas_limit)
    if gas_used > target:
        numerator = base_fee * (gas_used - target)
        # two floor divisions, as deployed
        delta = (numerator // target) // DENOMINATOR
        # minimum +1 wei only when parent fee is positive
        if delta == 0 and base_fee > 0:
            delta = 1
        return base_fee + delta
    if gas_used < target:
        numerator = base_fee * (target - gas_used)
        delta = (numerator // target) // DENOMINATOR
        return max(MIN_BASE_FEE, base_fee - delta)
    return base_fee


def oracle_fee_step(base_fee: int, gas_used: int, gas_limit: int) -> dict:
    target = oracle_target(gas_limit)
    if gas_used == target:
        direction, num, first, final = "flat", 0, 0, 0
    elif gas_used > target:
        num = base_fee * (gas_used - target)
        first = num // target
        final = first // DENOMINATOR
        if final == 0 and base_fee > 0:
            final = 1
        direction = "up"
    else:
        num = base_fee * (target - gas_used)
        first = num // target
        final = first // DENOMINATOR
        direction = "down"
    nxt = oracle_next_base_fee(base_fee, gas_used, gas_limit)
    return {
        "parent_base_fee": base_fee,
        "gas_used": gas_used,
        "gas_limit": gas_limit,
        "target_gas": target,
        "direction": direction,
        "delta_numerator": num,
        "delta_after_first_floor": first,
        "delta_final": final,
        "next_base_fee": nxt,
    }


def oracle_effective_tip(base_fee: int, max_fee: int, max_priority: int) -> int:
    return min(max_priority, max_fee - base_fee)


def oracle_effective_price(base_fee: int, max_fee: int, max_priority: int) -> int:
    return base_fee + oracle_effective_tip(base_fee, max_fee, max_priority)


# --------------------------------------------------------------------------- #
# Independent transaction economics + validity categorization
# --------------------------------------------------------------------------- #
def oracle_tx_economics(*, base_fee: int, gas_limit: int, max_fee: int,
                        max_priority: int, value: int, balance: int,
                        nonce: int, expected_nonce: int) -> dict:
    if max_fee < base_fee:
        return {"valid": False, "error_code": "E020_MAX_FEE_BELOW_BASE"}
    if gas_limit < INTRINSIC_GAS:
        return {"valid": False, "error_code": "E030_GAS_LIMIT_TOO_LOW"}
    tip = oracle_effective_tip(base_fee, max_fee, max_priority)
    price = base_fee + tip
    fee_total = price * gas_limit
    total = fee_total + value
    if total > 2**256 - 1 or max_fee * gas_limit > 2**256 - 1:
        return {"valid": False, "error_code": "E032_FEE_OVERFLOW"}
    if nonce != expected_nonce:
        return {"valid": False, "error_code": "E031_NONCE_MISMATCH"}
    if balance < total:
        return {"valid": False, "error_code": "E033_INSUFFICIENT_BALANCE"}
    return {
        "valid": True,
        "error_code": None,
        "tip": tip,
        "price": price,
        "burned": base_fee * gas_limit,
        "tipped": tip * gas_limit,
        "total_cost": total,
        "slack_refund": (max_fee - price) * gas_limit,
    }


# --------------------------------------------------------------------------- #
# Independent signing / addressing (direct ecdsa, own RLP)
# --------------------------------------------------------------------------- #
def oracle_address(pubkey_xy: bytes) -> str:
    return "0x" + hashlib.sha256(pubkey_xy).digest()[-20:].hex()


def oracle_key(seed: int) -> SigningKey:
    return SigningKey.from_secret_exponent(seed, curve=SECP256k1)


def oracle_pub_raw(sk: SigningKey) -> bytes:
    p = sk.verifying_key.pubkey.point
    return p.x().to_bytes(32, "big") + p.y().to_bytes(32, "big")


def _tx_fields(chain_id, nonce, max_fee, max_tip, gas, to: bytes, value, data):
    return [_scalar(chain_id), _scalar(nonce), _scalar(max_fee), _scalar(max_tip),
            _scalar(gas), to, _scalar(value), data]


def oracle_sign(sk: SigningKey, *, nonce: int, max_fee: int, max_tip: int,
                gas_limit: int, to: bytes, value: int, data: bytes = b"",
                chain_id: int = CHAIN_ID) -> dict:
    payload = rlp_encode(_tx_fields(chain_id, nonce, max_fee, max_tip,
                                    gas_limit, to, value, data))
    digest = hashlib.sha256(payload).digest()
    r, s = sigdecode_string(
        sk.sign_digest_deterministic(digest, hashfunc=hashlib.sha256,
                                     sigencode=sigencode_string),
        SECP256k1.order,
    )
    # low-s normalize
    flipped = False
    if s > HALF_N:
        s = SECP256k1.order - s
        flipped = True
    # v = index in ecdsa's candidate list (NOT y-parity; see project crypto docs)
    cands = VerifyingKey.from_public_key_recovery_with_digest(
        r.to_bytes(32, "big") + s.to_bytes(32, "big"), digest, curve=SECP256k1)
    signer = sk.verifying_key.pubkey.point
    v = None
    for idx, cand in enumerate(cands):
        if cand.pubkey.point == signer:
            v = idx
    assert v is not None
    return {"r": r, "s": s, "v": v, "digest": digest.hex(), "flipped_s": flipped}
