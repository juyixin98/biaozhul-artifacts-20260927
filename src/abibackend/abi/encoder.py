"""ABI encoder (head/tail, relative offsets).

``encode_value(header, value)`` returns a *self-contained* ABI block for one
value. For a dynamic container the block is ``head | tail``; for a static type
it is exactly its static words. Container offsets are emitted **relative to the
start of that container's head** — never absolute byte positions in the outer
blob. Nesting therefore composes by concatenation without rewriting offsets.
"""
from __future__ import annotations

from typing import Any, List, Sequence, Tuple

from .errors import ValueOutOfRange
from .types import (
    WORD,
    AddressType,
    BoolType,
    BytesType,
    DynamicArrayType,
    FixedArrayType,
    FixedBytesType,
    IntType,
    StringType,
    TupleType,
    TypeHeader,
    UintType,
    parse_type,
)


def _pad_left(data: bytes, fill: bytes) -> bytes:
    if len(data) > WORD:
        raise ValueOutOfRange("element longer than 32 bytes")
    return fill * (WORD - len(data)) + data


def _encode_uint(value: Any, bits: int, signed: bool) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueOutOfRange(f"expected int, got {type(value).__name__}")
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if value < lo or value > hi:
        raise ValueOutOfRange(f"{value} out of range for {'int' if signed else 'uint'}{bits}")
    # Two's-complement canonical 32-byte word; sign extends negatives.
    masked = value & ((1 << 256) - 1)
    return masked.to_bytes(WORD, "big", signed=False)


def _encode_bool(value: Any) -> bytes:
    if not isinstance(value, bool):
        raise ValueOutOfRange(f"expected bool, got {type(value).__name__}")
    return (b"\x01" if value else b"\x00").rjust(WORD, b"\x00")


def _encode_address(value: Any) -> bytes:
    # Accept int (preferred), 0x-hex (20 bytes), or raw bytes (20).
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0 or value > (1 << 160) - 1:
            raise ValueOutOfRange("address out of 160-bit range")
        return value.to_bytes(WORD, "big")
    if isinstance(value, str):
        s = value[2:] if value.startswith("0x") else value
        try:
            raw = bytes.fromhex(s)
        except ValueError as exc:
            raise ValueOutOfRange(f"bad address hex: {value!r}") from exc
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise ValueOutOfRange(f"unsupported address form: {type(value).__name__}")
    if len(raw) != 20:
        raise ValueOutOfRange(f"address must be 20 bytes, got {len(raw)}")
    return raw.rjust(WORD, b"\x00")


def _encode_fixed_bytes(value: Any, length: int) -> bytes:
    if isinstance(value, str):
        s = value[2:] if value.startswith("0x") else value
        try:
            raw = bytes.fromhex(s)
        except ValueError as exc:
            raise ValueOutOfRange("bad bytes hex") from exc
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise ValueOutOfRange(f"expected bytes for bytes{length}")
    if len(raw) != length:
        raise ValueOutOfRange(f"bytes{length} needs exactly {length} bytes, got {len(raw)}")
    # bytesN is right-padded to the word (data left, zeros right).
    return raw.ljust(WORD, b"\x00")


def _encode_prefixed_body(body: bytes) -> bytes:
    length = len(body)
    return length.to_bytes(WORD, "big") + body + b"\x00" * ((-length) % WORD)


def _as_sequence(value: Any, header: TypeHeader) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueOutOfRange(f"expected sequence for {header.canonical()}")
    if not isinstance(value, (list, tuple)):
        raise ValueOutOfRange(f"expected sequence for {header.canonical()}")
    return value


def _encode_container(header: TypeHeader, value: Any) -> bytes:
    """Encode a tuple/array whose dynamic-ness the caller already knows.

    Returns a self-contained ``head|tail`` block.
    """
    if isinstance(header, TupleType):
        children = header.components
        seq = _tuple_value(header, value)
        if len(seq) != len(children):
            raise ValueOutOfRange(
                f"tuple arity {len(children)} but got {len(seq)} values"
            )
    elif isinstance(header, DynamicArrayType):
        seq = _as_sequence(value, header)
        children = [header.element] * len(seq)
    elif isinstance(header, FixedArrayType):
        seq = _as_sequence(value, header)
        if len(seq) != header.length:
            raise ValueOutOfRange(
                f"{header.canonical()} needs {header.length} elements, got {len(seq)}"
            )
        children = [header.element] * header.length
    else:  # pragma: no cover
        raise TypeError(f"not a container: {header!r}")

    body = _layout(children, seq, prefix_words=1 if isinstance(header, DynamicArrayType) else 0)
    if isinstance(header, DynamicArrayType):
        # Dynamic arrays carry an explicit length word; fixed arrays do not.
        # Element offsets in ``body`` already count this word (prefix_words=1).
        return len(seq).to_bytes(WORD, "big") + body
    return body


def _tuple_value(header: TupleType, value: Any) -> Sequence[Any]:
    if isinstance(value, dict):
        # Allow {component_index: value}; named tuple members are not part of
        # the encoding, so positional is canonical. Accept mapping by index.
        if all(isinstance(k, int) for k in value.keys()):
            return [value[i] for i in range(len(header.components))]
        raise ValueOutOfRange("tuple mapping must be keyed by integer index")
    return _as_sequence(value, header)


def encode_value(header: TypeHeader, value: Any) -> bytes:
    if isinstance(header, UintType):
        return _encode_uint(value, header.bits, signed=False)
    if isinstance(header, IntType):
        return _encode_uint(value, header.bits, signed=True)
    if isinstance(header, BoolType):
        return _encode_bool(value)
    if isinstance(header, AddressType):
        return _encode_address(value)
    if isinstance(header, FixedBytesType):
        return _encode_fixed_bytes(value, header.length)
    if isinstance(header, BytesType):
        if isinstance(value, str):
            s = value[2:] if value.startswith("0x") else value
            try:
                raw = bytes.fromhex(s)
            except ValueError as exc:
                raise ValueOutOfRange("bad bytes hex") from exc
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
        else:
            raise ValueOutOfRange("bytes expects bytes or 0x-hex string")
        return _encode_prefixed_body(raw)
    if isinstance(header, StringType):
        if not isinstance(value, str):
            raise ValueOutOfRange("string expects str")
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as exc:  # pragma: no guard - str is already unicode
            raise ValueOutOfRange("string must be valid UTF-8") from exc
        return _encode_prefixed_body(raw)
    if isinstance(header, (TupleType, DynamicArrayType, FixedArrayType)):
        return _encode_container(header, value)
    raise TypeError(f"cannot encode {header!r}")  # pragma: no cover


def encode(types: Sequence[Any], values: Sequence[Any]) -> bytes:
    """Encode a top-level argument list as a head/tail sequence.

    Even a single dynamic argument occupies a pointer word in the head (its
    body begins at offset 0x20); this matches canonical ABI / ``eth_abi``.
    """
    types = list(types)
    values = list(values)
    if len(types) != len(values):
        raise ValueOutOfRange(f"arity {len(types)} != values {len(values)}")
    headers = [_coerce(t) for t in types]
    return _layout(headers, values)


def _head_words(header: TypeHeader) -> int:
    """Words a value occupies in a *parent* head: 1 for any dynamic value."""
    return 1 if header.is_dynamic else header.static_size_words()


def _layout(headers: Sequence[TypeHeader], values: Sequence[Any], prefix_words: int = 0) -> bytes:
    blocks: List[bytes] = [encode_value(h, v) for h, v in zip(headers, values)]
    head_slots = [_head_words(h) for h in headers]
    # Offsets are relative to the start of the *element head*. For a dynamic
    # array the length word sits immediately before that head, so the first
    # dynamic body begins at head_size + prefix_words*32.
    head_size = WORD * (sum(head_slots) + prefix_words)
    head = bytearray()
    tail = bytearray()
    cursor = head_size
    for child, block, nwords in zip(headers, blocks, head_slots):
        if child.is_dynamic:
            head += cursor.to_bytes(WORD, "big")
            tail += block
            cursor += len(block)
        else:
            head += block
    return bytes(head + tail)


def _coerce(spec: Any) -> TypeHeader:
    return spec if isinstance(spec, TypeHeader) else parse_type(spec)


def function_selector(name: str, types: Sequence[Any]) -> bytes:
    """First 4 bytes of keccak256("name(t1,t2)") via the mature Keccak backend."""
    from ..crypto import keccak256

    signature = name + "(" + ",".join(_coerce(t).canonical() for t in types) + ")"
    return keccak256(signature.encode("ascii"))[:4]


def encode_call(name: str, types: Sequence[Any], values: Sequence[Any]) -> bytes:
    return function_selector(name, types) + encode(types, values)
