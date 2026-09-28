"""Wire (JSON / RLP) <-> kernel transaction conversion.

Two input encodings are supported:

* ``structured``: explicit JSON fields plus a ``signature`` object,
* ``raw``: an 0x-hex RLP list with the signed EIP-1559-style field layout.

Wei quantities cross the wire as decimal *strings* (they can exceed
2**53); gas and nonce are plain JSON integers.
"""

from __future__ import annotations

from ..encoding import rlp
from ..encoding.hexutil import hex_to_bytes, decode_int, HexError
from ..kernel.models import Transaction, Signature
from ..errors import ErrorCode


class WireError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _as_int(value, field: str) -> int:
    if isinstance(value, bool):
        raise WireError(ErrorCode.E022_BAD_FIELDS.value, f"{field} must be integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s, 10)
        except ValueError:
            raise WireError(ErrorCode.E022_BAD_FIELDS.value,
                            f"{field} must be a decimal string")
    raise WireError(ErrorCode.E022_BAD_FIELDS.value, f"{field} must be integer")


def _data_bytes(value) -> bytes:
    if value is None:
        return b""
    if isinstance(value, str):
        if value == "":
            return b""
        try:
            return hex_to_bytes(value if value.startswith("0x") else "0x" + value)
        except HexError as exc:
            raise WireError(getattr(exc, "code", ErrorCode.E001_HEX_DECODE.value),
                            f"bad data hex: {exc}")
    raise WireError(ErrorCode.E001_HEX_DECODE.value, "data must be 0x-hex string")


def _addr(value, field="to") -> str:
    if not isinstance(value, str):
        raise WireError(ErrorCode.E001_HEX_DECODE.value, f"{field} must be hex string")
    raw = value[2:] if value.startswith("0x") else value
    if len(raw) != 40:
        raise WireError(ErrorCode.E001_HEX_DECODE.value, f"{field} must be 20 bytes")
    try:
        int(raw, 16)
    except ValueError:
        raise WireError(ErrorCode.E001_HEX_DECODE.value, f"{field} has non-hex chars")
    return "0x" + raw.lower()


def structured_to_transaction(obj: dict) -> Transaction:
    required = ("nonce", "max_fee_per_gas", "max_priority_fee_per_gas",
                "gas_limit", "to", "value", "signature")
    for key in required:
        if key not in obj:
            raise WireError(ErrorCode.E022_BAD_FIELDS.value, f"missing field: {key}")
    sig_obj = obj["signature"]
    if not isinstance(sig_obj, dict) or any(k not in sig_obj for k in ("r", "s", "v")):
        raise WireError(ErrorCode.E010_SIGNATURE_MALFORMED.value,
                        "signature must contain r,s,v")
    r, s, v = _as_int(sig_obj["r"], "r"), _as_int(sig_obj["s"], "s"), _as_int(sig_obj["v"], "v")
    return Transaction(
        chain_id=_as_int(obj["chain_id"], "chain_id") if obj.get("chain_id") is not None
        else _chain_default(),
        nonce=_as_int(obj["nonce"], "nonce"),
        max_fee_per_gas=_as_int(obj["max_fee_per_gas"], "max_fee_per_gas"),
        max_priority_fee_per_gas=_as_int(
            obj["max_priority_fee_per_gas"], "max_priority_fee_per_gas"),
        gas_limit=_as_int(obj["gas_limit"], "gas_limit"),
        to=_addr(obj["to"]),
        value=_as_int(obj["value"], "value"),
        data=_data_bytes(obj.get("data")),
        signature=Signature(r=r, s=s, v=v),
    )


def _chain_default() -> int:
    from ..params import PARAMS
    return PARAMS.chain_id


def raw_to_transaction(raw_hex: str) -> Transaction:
    try:
        raw = hex_to_bytes(raw_hex)
        items = rlp.decode_list(raw)
    except HexError as exc:
        raise WireError(getattr(exc, "code", ErrorCode.E001_HEX_DECODE.value), str(exc))
    except rlp.RLPError as exc:
        raise WireError(ErrorCode.E002_RLP_DECODE.value, str(exc))
    if len(items) != 11:
        raise WireError(ErrorCode.E002_RLP_DECODE.value,
                        f"signed tx RLP list must have 11 fields, got {len(items)}")
    try:
        chain_id, nonce, max_fee, max_tip, gas, to, value, data, r, s, v = items
        return Transaction(
            chain_id=decode_int(chain_id), nonce=decode_int(nonce),
            max_fee_per_gas=decode_int(max_fee),
            max_priority_fee_per_gas=decode_int(max_tip),
            gas_limit=decode_int(gas),
            to="0x" + to.hex(), value=decode_int(value), data=bytes(data),
            signature=Signature(r=decode_int(r), s=decode_int(s), v=decode_int(v)),
        )
    except HexError as exc:
        raise WireError(exc.code, str(exc))


def transaction_to_wire(tx: Transaction, tx_hash: str | None = None) -> dict:
    out = {
        "chain_id": tx.chain_id,
        "nonce": tx.nonce,
        "max_fee_per_gas": str(tx.max_fee_per_gas),
        "max_priority_fee_per_gas": str(tx.max_priority_fee_per_gas),
        "gas_limit": tx.gas_limit,
        "to": tx.to,
        "value": str(tx.value),
        "data": "0x" + tx.data.hex(),
    }
    if tx.signature is not None:
        out["signature"] = {"r": str(tx.signature.r), "s": str(tx.signature.s),
                            "v": tx.signature.v}
    if tx_hash is not None:
        out["tx_hash"] = tx_hash
    return out
