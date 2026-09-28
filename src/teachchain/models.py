"""交易与收据的数据模型与（去）序列化。

交易体（被签名覆盖的部分）::

    {
      "type": "deploy" | "invoke",
      "from": "0x....",
      "nonce": 7,
      "gas_limit": 100000,
      "to": "0x...." | null,      # invoke 必填
      "code_b64": "..." | null,   # deploy 必填
      "input": [1, 2, 3]          # invoke 输入字（deploy 为 []）
    }

签名信封::

    {"tx": <交易体>, "pub_b64": "<base64 Raw 公钥>", "sig_b64": "<base64 签名>"}

字段全部为 JSON 原生类型 / base64 文本，便于跨进程、跨语言复核摘要。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from . import crypto
from .errors import Rejected

VALID_TYPES = ("deploy", "invoke")
MAX_INPUT_WORDS = 256
MAX_CODE_BYTES = 24 * 1024
MAX_U64 = (1 << 64) - 1


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _require_bool(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise Rejected(code, message)


def normalize_tx_body(body: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化交易体；非法时抛 :class:`Rejected`。"""
    _require_bool(isinstance(body, dict), "bad_tx", "tx must be an object")
    tx_type = body.get("type")
    _require_bool(tx_type in VALID_TYPES, "bad_tx", f"type must be one of {VALID_TYPES}")
    sender = body.get("from")
    _require_bool(isinstance(sender, str) and sender.startswith("0x") and len(sender) == 18,
                  "bad_tx", "from must be 0x + 16 hex chars")
    try:
        int(sender[2:], 16)
    except ValueError:
        raise Rejected("bad_tx", "from must be hex") from None

    nonce = body.get("nonce")
    _require_bool(isinstance(nonce, int) and not isinstance(nonce, bool) and nonce >= 0,
                  "bad_tx", "nonce must be a non-negative integer")
    gas_limit = body.get("gas_limit")
    _require_bool(isinstance(gas_limit, int) and not isinstance(gas_limit, bool) and gas_limit > 0,
                  "bad_tx", "gas_limit must be a positive integer")

    norm: dict[str, Any] = {
        "type": tx_type,
        "from": sender.lower(),
        "nonce": nonce,
        "gas_limit": gas_limit,
        "to": None,
        "code_b64": None,
        "input": [],
    }

    if tx_type == "deploy":
        code_b64 = body.get("code_b64")
        _require_bool(isinstance(code_b64, str) and code_b64, "bad_tx",
                      "deploy requires code_b64")
        try:
            code = b64d(code_b64)
        except Exception:
            raise Rejected("bad_code_b64", "code_b64 is not valid base64") from None
        _require_bool(len(code) <= MAX_CODE_BYTES, "code_too_large",
                      f"code exceeds {MAX_CODE_BYTES} bytes")
        norm["code_b64"] = b64e(code)
    else:
        to = body.get("to")
        _require_bool(isinstance(to, str) and to.startswith("0x") and len(to) == 18,
                      "bad_tx", "invoke requires to = 0x + 16 hex chars")
        try:
            int(to[2:], 16)
        except ValueError:
            raise Rejected("bad_tx", "to must be hex") from None
        norm["to"] = to.lower()
        words = body.get("input", [])
        _require_bool(isinstance(words, list), "bad_tx", "input must be a list of words")
        _require_bool(len(words) <= MAX_INPUT_WORDS, "input_too_large",
                      f"input exceeds {MAX_INPUT_WORDS} words")
        clean: list[int] = []
        for w in words:
            _require_bool(isinstance(w, int) and not isinstance(w, bool) and 0 <= w <= MAX_U64,
                          "bad_tx", "input words must be u64 integers")
            clean.append(w)
        norm["input"] = clean
    return norm


def tx_digest(body: dict[str, Any]) -> str:
    """交易摘要：对规范化交易体做 SHA-256。结果绑定全部输入。"""
    return crypto.digest_payload(body)


def verify_envelope(envelope: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """校验签名信封，返回 (规范化交易体, tx_hash)。

    公钥以 base64(Raw Ed25519) 给出；地址必须与公钥派生地址一致，防止冒名。
    """
    _require_bool(isinstance(envelope, dict), "bad_envelope", "envelope must be an object")
    body = normalize_tx_body(envelope.get("tx"))
    pub_b64 = envelope.get("pub_b64")
    sig_b64 = envelope.get("sig_b64")
    _require_bool(isinstance(pub_b64, str) and isinstance(sig_b64, str),
                  "bad_signature", "pub_b64 and sig_b64 are required strings")
    try:
        pub_raw = base64.b64decode(pub_b64, validate=True)
        sig = base64.b64decode(sig_b64, validate=True)
    except Exception:
        raise Rejected("bad_signature", "pub_b64/sig_b64 are not valid base64") from None
    if len(pub_raw) != 32:
        raise Rejected("bad_signature", "Ed25519 public key must be 32 bytes")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        pub = Ed25519PublicKey.from_public_bytes(pub_raw)
        digest = tx_digest(body)
        pub.verify(sig, bytes.fromhex(digest))
    except InvalidSignature:
        raise Rejected("bad_signature",
                       "signature does not verify against tx digest") from None
    except ValueError as exc:
        raise Rejected("bad_signature", f"malformed signature: {exc}") from None

    derived = crypto.address_from_public_key(pub)
    _require_bool(derived == body["from"], "sender_mismatch",
                  f"from={body['from']} but pubkey derives {derived}")
    return body, digest
