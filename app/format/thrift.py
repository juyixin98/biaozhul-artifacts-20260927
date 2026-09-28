"""Minimal Thrift compact protocol codec.

Implements just enough of the Apache Thrift compact protocol
(https://github.com/apache/thrift/blob/master/doc/specs/thrift-compact-protocol.md)
to read and write the Parquet metadata structs used by this project. Structs
are described as plain Python field tables so no code generation is needed.

Field descriptors: ``(id, name, type)`` where type is one of the CT_* compact
type constants, or ``('struct', TABLE)``, ``('list', ELEM_TYPE)``,
``('enum',)`` (encoded as i32).
"""
from __future__ import annotations

from dataclasses import dataclass

CT_STOP = 0
CT_TRUE = 1
CT_FALSE = 2
CT_BYTE = 3
CT_I16 = 4
CT_I32 = 5
CT_I64 = 6
CT_DOUBLE = 8
CT_BINARY = 8 | 0  # placeholder (overridden below)
CT_BINARY = 8       # noqa: F811
CT_STRUCT = 12
CT_LIST = 9
CT_I32 = 5
CT_I64 = 6

# Re-declare distinct constants (compact protocol uses type id 8 for BINARY).
T_STOP = 0
T_BOOLEAN_TRUE = 1
T_BOOLEAN_FALSE = 2
T_BYTE = 3
T_I16 = 4
T_I32 = 5
T_I64 = 6
T_DOUBLE = 7
T_BINARY = 8
T_LIST = 9
T_STRUCT = 12


@dataclass(frozen=True)
class Field:
    id: int
    name: str
    type: object  # int compact type or composite descriptor


@dataclass(frozen=True)
class StructSpec:
    name: str
    fields: tuple[Field, ...]

    def by_id(self) -> dict[int, Field]:
        return {f.id: f for f in self.fields}

    def by_name(self) -> dict[str, Field]:
        return {f.name: f for f in self.fields}


@dataclass(frozen=True)
class ListOf:
    elem: object


@dataclass(frozen=True)
class StructOf:
    spec: "StructSpec"


# --------------------------------------------------------------------------- #
# ZigZag varint
# --------------------------------------------------------------------------- #

def write_varint(buf: bytearray, value: int) -> None:
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def zigzag_encode(n: int, bits: int = 64) -> int:
    return ((n << 1) ^ (n >> (bits - 1))) & ((1 << bits) - 1)


def zigzag_decode(n: int, bits: int = 64) -> int:
    mask = (1 << bits) - 1
    n &= mask
    return (n >> 1) ^ -(n & 1)


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

class CompactWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write_struct(self, spec: StructSpec, value: dict) -> None:
        by_name = spec.by_name()
        last_id = 0
        # Emit in schema order for determinism; skip absent/None fields.
        for field in spec.fields:
            if field.name not in value or value[field.name] is None:
                continue
            fid = field.id
            delta = fid - last_id
            if 0 < delta <= 15:
                self.buf.append((delta << 4) | _compact_type_byte(field.type))
            else:
                self.buf.append(_compact_type_byte(field.type))
                write_varint(self.buf, zigzag_encode(fid, bits=16))
            self._write_value(field.type, value[field.name])
            last_id = fid
        self.buf.append(T_STOP)

    def _write_value(self, ftype, value) -> None:
        if isinstance(ftype, StructOf):
            self.write_struct(ftype.spec, value)
            return
        if isinstance(ftype, ListOf):
            self._write_list(ftype.elem, value)
            return
        if ftype == T_BOOLEAN_TRUE:
            self.buf.append(1 if value else 0)
            return
        if ftype == T_I32 or ftype == T_I16 or ftype == T_BYTE:
            write_varint(self.buf, zigzag_encode(int(value)) & 0xFFFFFFFFFFFFFFFF)
            return
        if ftype == T_I64:
            write_varint(self.buf, zigzag_encode(int(value)) & 0xFFFFFFFFFFFFFFFF)
            return
        if ftype == T_DOUBLE:
            import struct
            self.buf += struct.pack("<d", float(value))
            return
        if ftype == T_BINARY:
            data = value if isinstance(value, (bytes, bytearray)) else value.encode("utf-8")
            write_varint(self.buf, len(data))
            self.buf += data
            return
        raise ValueError(f"cannot write compact type {ftype!r}")

    def _write_list(self, elem_type, values) -> None:
        n = len(values)
        if n <= 14:
            self.buf.append((n << 4) | _compact_type_byte(elem_type))
        else:
            self.buf.append(0xF0 | _compact_type_byte(elem_type))
            write_varint(self.buf, n)
        for v in values:
            self._write_value(elem_type, v)


def _compact_type_byte(ftype) -> int:
    if isinstance(ftype, StructOf):
        return T_STRUCT
    if isinstance(ftype, ListOf):
        return T_LIST
    if ftype in (T_I32, T_I16, T_BYTE, T_I64):
        return ftype
    return ftype


# --------------------------------------------------------------------------- #
# Reader
# --------------------------------------------------------------------------- #

class CompactReader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def read_struct(self, spec: StructSpec) -> dict:
        result: dict[str, object] = {}
        last_id = 0
        by_id = spec.by_id()
        while self.pos < self.end:
            b = self.data[self.pos]
            self.pos += 1
            if b == T_STOP:
                break
            delta = (b >> 4) & 0x0F
            ctype = b & 0x0F
            if delta == 0:
                raw_fid, self.pos = read_varint(self.data, self.pos)
                fid = zigzag_decode(raw_fid, bits=16)
            else:
                fid = last_id + delta
            last_id = fid
            field = by_id.get(fid)
            if field is None:
                self._skip_value(ctype)
                continue
            result[field.name] = self._read_value(field.type, ctype)
        return result

    def _read_value(self, ftype, ctype: int):
        if isinstance(ftype, StructOf):
            return self.read_struct(ftype.spec)
        if isinstance(ftype, ListOf):
            return self._read_list(ftype.elem, ctype)
        if ctype == T_BOOLEAN_TRUE:
            return True
        if ctype == T_BOOLEAN_FALSE:
            return False
        if ctype in (T_I32, T_I64, T_I16, T_BYTE):
            raw, self.pos = read_varint(self.data, self.pos)
            # Compact protocol zigzag-encodes every signed integer type.
            val = zigzag_decode(raw)
            if ctype in (T_I32, T_I16, T_BYTE):
                val &= 0xFFFFFFFF
                if val >= 0x80000000:
                    val -= 0x100000000
            elif ctype == T_I64:
                val &= 0xFFFFFFFFFFFFFFFF
                if val >= 0x8000000000000000:
                    val -= 0x10000000000000000
            return val
        if ctype == T_DOUBLE:
            import struct
            (val,) = struct.unpack_from("<d", self.data, self.pos)
            self.pos += 8
            return val
        if ctype == T_BINARY:
            length, self.pos = read_varint(self.data, self.pos)
            s = self.data[self.pos:self.pos + length]
            self.pos += length
            return s
        raise ValueError(f"unsupported compact value type {ctype} at {self.pos}")

    def _read_list(self, elem_type, ctype: int):
        head = self.data[self.pos]
        self.pos += 1
        size = (head >> 4) & 0x0F
        ectype = head & 0x0F
        if size == 15:
            size, self.pos = read_varint(self.data, self.pos)
        out = []
        for _ in range(size):
            out.append(self._read_value(elem_type, ectype))
        return out

    def _skip_value(self, ctype: int) -> None:
        if ctype in (T_BOOLEAN_TRUE, T_BOOLEAN_FALSE, T_BYTE):
            self.pos += 1
        elif ctype in (T_I16, T_I32, T_I64):
            _, self.pos = read_varint(self.data, self.pos)
        elif ctype == T_DOUBLE:
            self.pos += 8
        elif ctype == T_BINARY:
            length, self.pos = read_varint(self.data, self.pos)
            self.pos += length
        elif ctype == T_STRUCT:
            depth = 1
            while depth:
                b = self.data[self.pos]
                self.pos += 1
                if b == T_STOP:
                    depth -= 1
                    continue
                d = (b >> 4) & 0x0F
                inner = b & 0x0F
                if d == 0:
                    _, self.pos = read_varint(self.data, self.pos)
                self._skip_value(inner)
        elif ctype == T_LIST:
            head = self.data[self.pos]
            self.pos += 1
            size = (head >> 4) & 0x0F
            ectype = head & 0x0F
            if size == 15:
                size, self.pos = read_varint(self.data, self.pos)
            for _ in range(size):
                self._skip_value(ectype)
        else:
            raise ValueError(f"cannot skip compact type {ctype}")


def encode_struct(spec: StructSpec, value: dict) -> bytes:
    w = CompactWriter()
    w.write_struct(spec, value)
    return bytes(w.buf)


def decode_struct(spec: StructSpec, data: bytes, pos: int = 0,
                  end: int | None = None) -> tuple[dict, int]:
    r = CompactReader(data, pos, end)
    val = r.read_struct(spec)
    return val, r.pos
