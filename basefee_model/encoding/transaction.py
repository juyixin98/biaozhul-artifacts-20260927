"""Synthetic transaction model: legacy (type 0) and EIP-1559 (type 2).

The model encodes the fields that matter for fee validity and conservation:

    nonce, gas_limit, to, value, data, chain_id
    legacy:  gas_price
    type-2:  max_fee_per_gas (fee cap), max_priority_fee_per_gas

It performs real RLP signing with EIP-155 / EIP-1559 message hashing and
recovers the sender from the signature rather than trusting a supplied sender.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coincurve import PrivateKey

from ..config import (MAX_U256, TX_TYPE_EIP1559, TX_TYPE_LEGACY)
from ..errors import EncodingError, FailureCode, SignatureError
from . import rlp
from .crypto import (keccak256, public_key_bytes, recover_public_key,
                     recoverable_sign, address_from_pubkey)


@dataclass
class Transaction:
    type: int
    nonce: int
    gas_limit: int
    to: bytes                 # 20-byte recipient (zero bytes = synthetic burn addr)
    value: int = 0
    data: bytes = b""
    chain_id: int = 1559
    # type-2 fields
    max_fee_per_gas: int = 0
    max_priority_fee_per_gas: int = 0
    # legacy field
    gas_price: int = 0
    # signature
    recovery_id: int = 0
    r: bytes = b""
    s: bytes = b""

    # -- field validation --------------------------------------------------
    def validate_fields(self) -> None:
        for name, v in (("nonce", self.nonce), ("gas_limit", self.gas_limit),
                        ("value", self.value)):
            if not isinstance(v, int) or v < 0:
                raise EncodingError(f"{name} must be a non-negative integer",
                                    code=FailureCode.INVALID_FIELDS)
        if self.type == TX_TYPE_EIP1559:
            for name, v in (("max_fee_per_gas", self.max_fee_per_gas),
                            ("max_priority_fee_per_gas",
                             self.max_priority_fee_per_gas)):
                if not isinstance(v, int) or v < 0:
                    raise EncodingError(f"{name} must be >= 0",
                                        code=FailureCode.INVALID_FIELDS)
        elif self.type == TX_TYPE_LEGACY:
            if not isinstance(self.gas_price, int) or self.gas_price < 0:
                raise EncodingError("gas_price must be >= 0",
                                    code=FailureCode.INVALID_FIELDS)
        else:
            raise EncodingError(f"unsupported tx type {self.type}",
                                code=FailureCode.UNSUPPORTED_TX_TYPE)
        if len(self.to) not in (0, 20):
            raise EncodingError("to must be 0 or 20 bytes",
                                code=FailureCode.INVALID_FIELDS)

    # -- hashing / encoding ------------------------------------------------
    def _legacy_signing_items(self) -> list:
        # EIP-155: rlp(nonce, gasPrice, gasLimit, to, value, data,
        #                chainId, 0, 0)
        return [
            rlp.int_to_bytes(self.nonce),
            rlp.int_to_bytes(self.gas_price),
            rlp.int_to_bytes(self.gas_limit),
            self.to,
            rlp.int_to_bytes(self.value),
            self.data,
            rlp.int_to_bytes(self.chain_id),
            b"", b"",
        ]

    def _eip1559_signing_items(self) -> list:
        # 0x02 || rlp(chainId, nonce, maxPrio, maxFee, gasLimit, to, value,
        #             data, [])
        return [
            rlp.int_to_bytes(self.chain_id),
            rlp.int_to_bytes(self.nonce),
            rlp.int_to_bytes(self.max_priority_fee_per_gas),
            rlp.int_to_bytes(self.max_fee_per_gas),
            rlp.int_to_bytes(self.gas_limit),
            self.to,
            rlp.int_to_bytes(self.value),
            self.data,
            [],
        ]

    def signing_hash(self) -> bytes:
        if self.type == TX_TYPE_LEGACY:
            return keccak256(rlp.encode(self._legacy_signing_items()))
        return keccak256(
            bytes([TX_TYPE_EIP1559]) + rlp.encode(self._eip1559_signing_items())
        )

    def sign(self, priv: PrivateKey) -> "Transaction":
        """Populate r/s/recovery_id in place and return self."""
        self.validate_fields()
        rid, rr, ss = recoverable_sign(self.signing_hash(), priv)
        self.recovery_id = rid
        self.r, self.s = rr, ss
        return self

    def sender(self) -> bytes:
        """Recover the 20-byte sender address from the signature."""
        digest = self.signing_hash()
        try:
            pub = recover_public_key(digest, self.r, self.s, self.recovery_id)
        except SignatureError:
            raise
        return address_from_pubkey(pub)

    def encoded(self) -> bytes:
        """The canonical on-wire transaction bytes (including signature)."""
        if not self.r or not self.s:
            raise SignatureError("transaction is unsigned",
                                 code=FailureCode.BAD_SIGNATURE)
        if self.type == TX_TYPE_LEGACY:
            # EIP-155 v = chainId*2 + 35 + recoveryId
            v = self.chain_id * 2 + 35 + self.recovery_id
            items = [
                rlp.int_to_bytes(self.nonce),
                rlp.int_to_bytes(self.gas_price),
                rlp.int_to_bytes(self.gas_limit),
                self.to,
                rlp.int_to_bytes(self.value),
                self.data,
                rlp.int_to_bytes(v),
                self.r,
                self.s,
            ]
            return rlp.encode(items)
        yparity = self.recovery_id  # type-2 uses yParity directly
        items = self._eip1559_signing_items() + [
            rlp.int_to_bytes(yparity), self.r, self.s,
        ]
        return bytes([TX_TYPE_EIP1559]) + rlp.encode(items)

    def tx_hash(self) -> bytes:
        return keccak256(self.encoded())

    # -- fee semantics -----------------------------------------------------
    def effective_gas_price(self, base_fee: int) -> int:
        """Effective gas price paid (EIP-1559 rule).

        type-2: min(max_fee, base_fee + priority)
        legacy: gas_price (treated as fee_cap = priority = gas_price, an
                explicit legacy/non-EIP-1559 uncertainty noted in the API).
        """
        if self.type == TX_TYPE_LEGACY:
            return self.gas_price
        return min(self.max_fee_per_gas, base_fee + self.max_priority_fee_per_gas)

    def priority_fee_per_gas(self, base_fee: int) -> int:
        if self.type == TX_TYPE_LEGACY:
            return max(0, self.gas_price - base_fee)
        return min(self.max_priority_fee_per_gas,
                   self.max_fee_per_gas - base_fee)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "nonce": self.nonce,
            "gas_limit": self.gas_limit,
            "to": "0x" + self.to.hex(),
            "value": self.value,
            "data": "0x" + self.data.hex(),
            "chain_id": self.chain_id,
            "max_fee_per_gas": self.max_fee_per_gas,
            "max_priority_fee_per_gas": self.max_priority_fee_per_gas,
            "gas_price": self.gas_price,
            "recovery_id": self.recovery_id,
            "r": "0x" + self.r.hex() if self.r else "",
            "s": "0x" + self.s.hex() if self.s else "",
            "tx_hash": "0x" + self.tx_hash().hex() if (self.r and self.s) else "",
        }


def decode_transaction(raw: bytes) -> Transaction:
    """Parse canonical transaction bytes (type-2 tagged or legacy RLP list)."""
    if not raw:
        raise EncodingError("empty transaction", code=FailureCode.MALFORMED_RLP)
    try:
        if raw[0] == TX_TYPE_EIP1559:
            items = rlp.decode(raw[1:])
            if not isinstance(items, list) or len(items) != 12:
                raise EncodingError("type-2 tx must have 12 fields",
                                    code=FailureCode.MALFORMED_RLP)
            (cid, nonce, prio, fee, gl, to, value, data, _access,
             yparity, rr, ss) = items
            tx = Transaction(
                type=TX_TYPE_EIP1559,
                chain_id=rlp.int_from_bytes(cid),
                nonce=rlp.int_from_bytes(nonce),
                max_priority_fee_per_gas=rlp.int_from_bytes(prio),
                max_fee_per_gas=rlp.int_from_bytes(fee),
                gas_limit=rlp.int_from_bytes(gl),
                to=to, value=rlp.int_from_bytes(value), data=data,
                recovery_id=rlp.int_from_bytes(yparity), r=rr, s=ss,
            )
        else:
            items = rlp.decode(raw)
            if not isinstance(items, list) or len(items) != 9:
                raise EncodingError("legacy tx must have 9 fields",
                                    code=FailureCode.MALFORMED_RLP)
            nonce, gp, gl, to, value, data, vb, rr, ss = items
            v = rlp.int_from_bytes(vb)
            # EIP-155: v = chainId*2 + 35 + yParity, yParity in {0,1}.
            if v < 35 or (v - 35) % 2 not in (0, 1) or (v - 35) // 2 < 1:
                raise EncodingError(
                    f"unsupported legacy v={v} (need EIP-155 v>=35)",
                    code=FailureCode.MALFORMED_RLP)
            cid = (v - 35) // 2
            rid = (v - 35) % 2
            tx = Transaction(
                type=TX_TYPE_LEGACY, chain_id=cid,
                nonce=rlp.int_from_bytes(nonce), gas_price=rlp.int_from_bytes(gp),
                gas_limit=rlp.int_from_bytes(gl), to=to,
                value=rlp.int_from_bytes(value), data=data,
                recovery_id=rid, r=rr, s=ss,
            )
    except EncodingError:
        raise
    except (IndexError, ValueError, TypeError) as exc:
        raise EncodingError(f"malformed transaction: {exc}",
                            code=FailureCode.MALFORMED_RLP) from exc

    tx.validate_fields()
    return tx
