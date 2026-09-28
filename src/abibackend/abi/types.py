"""Type system for the restricted ABI dialect.

Supported headers (Ethereum ABI, head/tail encoding):
    uint<8..256 step 8>, int<...>, bool, address
    bytes<n>  (1..32), bytes, string
    T[k] fixed arrays, T[] dynamic arrays, (T1,T2,...) tuples

Tuple components are themselves headers, so nested dynamic arrays/tuples work.
A header knows whether it is dynamic and its *static* size in words; dynamic
types report a static size of 1 (the offset pointer they occupy in a head).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Sequence, Union

from .errors import InvalidType, UnsupportedType

WORD = 32


class TypeHeader:
    # Dynamic per Solidity ABI: value's static region is one offset word.
    is_dynamic: bool = False

    def static_size_words(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def canonical(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.canonical()}>"


@dataclass(frozen=True)
class UintType(TypeHeader):
    bits: int

    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return f"uint{self.bits}"


@dataclass(frozen=True)
class IntType(TypeHeader):
    bits: int

    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return f"int{self.bits}"


@dataclass(frozen=True)
class BoolType(TypeHeader):
    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return "bool"


@dataclass(frozen=True)
class AddressType(TypeHeader):
    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return "address"


@dataclass(frozen=True)
class FixedBytesType(TypeHeader):
    length: int

    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return f"bytes{self.length}"


@dataclass(frozen=True)
class BytesType(TypeHeader):
    is_dynamic: bool = True

    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return "bytes"


@dataclass(frozen=True)
class StringType(TypeHeader):
    is_dynamic: bool = True

    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return "string"


@dataclass(frozen=True)
class FixedArrayType(TypeHeader):
    element: TypeHeader
    length: int
    is_dynamic: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "is_dynamic", self.element.is_dynamic)

    def static_size_words(self) -> int:
        return self.length * self.element.static_size_words()

    def canonical(self) -> str:
        return f"{self.element.canonical()}[{self.length}]"


@dataclass(frozen=True)
class DynamicArrayType(TypeHeader):
    element: TypeHeader
    is_dynamic: bool = True

    def static_size_words(self) -> int:
        return 1

    def canonical(self) -> str:
        return f"{self.element.canonical()}[]"


@dataclass(frozen=True)
class TupleType(TypeHeader):
    components: List[TypeHeader]
    is_dynamic: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "is_dynamic", any(c.is_dynamic for c in self.components))

    def static_size_words(self) -> int:
        return sum(c.static_size_words() for c in self.components)

    def canonical(self) -> str:
        return "(" + ",".join(c.canonical() for c in self.components) + ")"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
class _Cursor:
    __slots__ = ("s", "i")

    def __init__(self, s: str, i: int = 0):
        self.s = s
        self.i = i

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def take(self) -> str:
        ch = self.peek()
        self.i += 1
        return ch

    def expect(self, ch: str) -> None:
        if self.take() != ch:
            raise InvalidType(f"expected {ch!r} at {self.i - 1} in {self.s!r}")

    def eof(self) -> bool:
        return self.i >= len(self.s)


def _parse_int_kind(word: str) -> TypeHeader:
    if word in ("uint", "int"):
        # Bare uint/int are aliases for 256 in Solidity but we require an
        # explicit width in this restricted dialect to avoid ambiguity.
        raise InvalidType(f"explicit width required: {word}")
    for prefix, cls in (("uint", UintType), ("int", IntType)):
        if word.startswith(prefix):
            num = word[len(prefix):]
            if not num.isdigit():
                raise InvalidType(f"bad width in {word!r}")
            bits = int(num)
            if bits < 8 or bits > 256 or bits % 8 != 0:
                raise UnsupportedType(f"width must be 8..256 in steps of 8: {word}")
            return cls(bits=bits)
    raise InvalidType(f"unknown elementary type {word!r}")


def _read_name(cur: _Cursor) -> str:
    start = cur.i
    # Elementary names are letters only; digits belong to a width suffix
    # (uint256 is handled by _parse_int_kind after this, so read letters then
    # hand the digits back). We read letters here, and integer kinds read the
    # remainder from the cursor.
    while cur.peek().isalpha() and not cur.eof():
        cur.take()
    # For uint/int the width digits follow; include them for those keywords.
    word = cur.s[start:cur.i]
    if word in ("uint", "int"):
        dstart = cur.i
        while cur.peek().isdigit():
            cur.take()
        word += cur.s[dstart:cur.i]
    return word


def _parse_header(cur: _Cursor) -> TypeHeader:
    ch = cur.peek()
    if ch == "(":
        header: TypeHeader = _parse_tuple(cur)
    else:
        word = _read_name(cur)
        if word == "bool":
            header = BoolType()
        elif word == "address":
            header = AddressType()
        elif word == "bytes":
            # Could be "bytes" or "bytes<n>"; look ahead for digits without
            # consuming array suffixes.
            j = cur.i
            digits = ""
            while j < len(cur.s) and cur.s[j].isdigit():
                digits += cur.s[j]
                j += 1
            if digits:
                cur.i = j
                n = int(digits)
                if n < 1 or n > 32:
                    raise UnsupportedType(f"bytes length must be 1..32: bytes{n}")
                header = FixedBytesType(length=n)
            else:
                header = BytesType()
        elif word == "string":
            header = StringType()
        else:
            header = _parse_int_kind(word)
    # Array suffixes: [] or [k], repeatable.
    while cur.peek() == "[":
        cur.expect("[")
        digits = ""
        while cur.peek().isdigit():
            digits += cur.take()
        cur.expect("]")
        if digits == "":
            header = DynamicArrayType(element=header)
        else:
            k = int(digits)
            if k < 1:
                raise InvalidType("fixed array length must be >= 1 (zero-length is not ABI)")
            header = FixedArrayType(element=header, length=k)
    return header


def _parse_tuple(cur: _Cursor) -> TypeHeader:
    cur.expect("(")
    comps: List[TypeHeader] = []
    if cur.peek() == ")":
        cur.expect(")")
        return TupleType(components=[])
    while True:
        comps.append(_parse_header(cur))
        if cur.peek() == ",":
            cur.take()
            continue
        if cur.peek() == ")":
            cur.take()
            break
        raise InvalidType(f"expected ',' or ')' at {cur.i} in {cur.s!r}")
    return TupleType(components=comps)


def parse_type(spec: Union[str, Sequence[Any]]) -> TypeHeader:
    """Parse a canonical type string (e.g. ``"(uint256,bytes[])[2]"``).

    A list/tuple of component specs is accepted as shorthand for a tuple type.
    """
    if isinstance(spec, (list, tuple)):
        return TupleType(components=[parse_type(c) for c in spec])
    if not isinstance(spec, str) or spec == "":
        raise InvalidType(f"type spec must be non-empty string, got {spec!r}")
    cur = _Cursor(spec.strip())
    header = _parse_header(cur)
    if not cur.eof():
        raise InvalidType(f"trailing characters at {cur.i} in {spec!r}")
    return header


def type_from_json(component: dict) -> TypeHeader:
    """Build a header from a Solidity/JSON ABI component descriptor.

    Example::

        {"type": "tuple[]", "components": [{"type":"uint256"}, {"type":"bytes"}]}
    """
    kind = component.get("type", "")
    suffixes = ""
    # Peel array suffixes off and re-attach after parsing the base type.
    base = kind
    while base.endswith("]"):
        open_i = base.rfind("[")
        if open_i == -1:
            raise InvalidType(f"malformed array type {kind!r}")
        suffixes = base[open_i:] + suffixes
        base = base[:open_i]

    if base == "tuple":
        comps = component.get("components")
        if not isinstance(comps, list):
            raise InvalidType("tuple type requires a 'components' list")
        header: TypeHeader = TupleType(components=[type_from_json(c) for c in comps])
    else:
        header = parse_type(base)

    if suffixes:
        # Reuse the canonical parser to attach suffixes deterministically.
        header = parse_type(header.canonical() + suffixes)
    return header
