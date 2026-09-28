"""Restricted Ethereum ABI codec.

Public surface::

    parse_type(spec)          -> ABIHeader
    encode_value(header, v)   -> bytes (self-contained ABI block)
    decode_value(header, blob)-> python value (strict, bounds-checked)
    encode(types, args)       -> bytes (head-tail tuple encoding)
    decode(types, blob)       -> tuple
    function_selector(...)    -> 4 bytes (mature Keccak)
    encode_call(...)          -> selector || args

The encoder/decoder are hand-written; the only cryptographic primitive
(Keccak-256) comes from a mature library (see :mod:`abibackend.crypto`).
"""
from __future__ import annotations

from .errors import (
    ABIError,
    DecodeError,
    InvalidType,
    LengthTooLarge,
    NonCanonicalEncoding,
    NonCanonicalPadding,
    OffsetOutOfBounds,
    OffsetOverlap,
    TrailingBytes,
    UnsupportedType,
    ValueOutOfRange,
)
from .types import (
    AddressType,
    BoolType,
    BytesType,
    FixedArrayType,
    FixedBytesType,
    IntType,
    StringType,
    TupleType,
    TypeHeader,
    UintType,
    parse_type,
    type_from_json,
)
from .encoder import encode, encode_call, encode_value, function_selector
from .decoder import decode, decode_value

__all__ = [
    "parse_type",
    "type_from_json",
    "TypeHeader",
    "UintType",
    "IntType",
    "BoolType",
    "AddressType",
    "FixedBytesType",
    "BytesType",
    "StringType",
    "FixedArrayType",
    "TupleType",
    "encode",
    "decode",
    "encode_value",
    "decode_value",
    "function_selector",
    "encode_call",
    "ABIError",
    "UnsupportedType",
    "InvalidType",
    "ValueOutOfRange",
    "NonCanonicalPadding",
    "OffsetOutOfBounds",
    "OffsetOverlap",
    "LengthTooLarge",
    "TrailingBytes",
    "NonCanonicalEncoding",
    "DecodeError",
]
