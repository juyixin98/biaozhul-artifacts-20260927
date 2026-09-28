"""Encoding & signing module: RLP, SHA-256 domain hashing, secp256k1 signatures."""

from .rlp import encode, decode, decode_list, RLPError
from .hexutil import (
    hex_to_bytes,
    bytes_to_hex,
    encode_int,
    decode_int,
    int_to_minimal_bytes,
    HexError,
)
from . import crypto, serialization

__all__ = [
    "encode",
    "decode",
    "decode_list",
    "RLPError",
    "hex_to_bytes",
    "bytes_to_hex",
    "encode_int",
    "decode_int",
    "int_to_minimal_bytes",
    "HexError",
    "crypto",
    "serialization",
]
