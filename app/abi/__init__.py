"""ABI encoding / verification core.

Public entry points:
    encode(types, values) -> bytes
    decode(types, data) -> tuple
    encode_call(selector_types, args) -> bytes
    decode_call(selector, data, reg) -> tuple
    function_selector(signature) -> bytes
"""

from .errors import (
    ABIDecodeError,
    ABIEncodeError,
    ABIError,
    ABIValueError,
    AllocationLimitError,
    DepthLimitError,
    InvalidTypeError,
    LengthMismatchError,
    NonCanonicalLayoutError,
    NonCanonicalPaddingError,
    OffsetOutOfBoundsError,
    OverlapError,
    UnsupportedTypeError,
)
from .codec import decode, encode
from .call import decode_call, encode_call, function_selector, selector_for

__all__ = [
    "encode",
    "decode",
    "encode_call",
    "decode_call",
    "function_selector",
    "selector_for",
    "ABIError",
    "ABIEncodeError",
    "ABIDecodeError",
    "ABIValueError",
    "InvalidTypeError",
    "UnsupportedTypeError",
    "OffsetOutOfBoundsError",
    "NonCanonicalPaddingError",
    "NonCanonicalLayoutError",
    "OverlapError",
    "LengthMismatchError",
    "AllocationLimitError",
    "DepthLimitError",
]
