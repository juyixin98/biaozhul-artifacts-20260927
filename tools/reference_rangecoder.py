#!/usr/bin/env python3
"""Independent reference implementation of the fixed-precision range coder.

This file is written from first principles (the integer interval contract)
and shares NO code with the Rust crate under test.  It is used two ways:

  * as an oracle: it emits the exact bytes the encoder must produce,
  * as a verifier: it decodes Rust-produced payloads symbol by symbol.

Contract (must match src/range.rs docs):
    TOP        = 1 << 24
    INIT_RANGE = 0xFFFFFFFF
    seed byte  = 0x00, then four code bytes
    low += (range // total) * cum ;  range = (range // total) * freq
    carry via pending 0xFF chain; five final ShiftLow() calls
"""

from dataclasses import dataclass, field
from typing import List, Tuple

TOP = 1 << 24
INIT_RANGE = 0xFFFFFFFF
MASK32 = 0xFFFFFFFF


class TableError(ValueError):
    pass


class FreqTable:
    def __init__(self, freqs: List[int], bound: int):
        if not freqs:
            raise TableError("empty alphabet")
        if not (1 <= bound <= 1 << 24):
            raise TableError("bad bound")
        total = 0
        for i, f in enumerate(freqs):
            if f > bound:
                raise TableError(f"entry {i} exceeds bound")
            total += f
            if total > bound:
                raise TableError(f"total exceeded at {i}")
        if total == 0:
            raise TableError("all zero")
        self.freqs = list(freqs)
        self.bound = bound
        self.total = total
        self.cum = [0]
        acc = 0
        for f in freqs:
            acc += f
            self.cum.append(acc)

    def interval(self, symbol: int) -> Tuple[int, int]:
        return self.cum[symbol], self.cum[symbol + 1]

    def symbol_for(self, value: int) -> int:
        # first index i with cum[i] > value, minus one
        lo, hi = 0, len(self.cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self.cum[mid + 1] > value:
                hi = mid
            else:
                lo = mid + 1
        return lo


class EncodeError(ValueError):
    pass


class RangeEncoder:
    def __init__(self):
        self.out = bytearray()
        self.cache = 0
        self.cache_size = 1
        self.low = 0
        self.range = INIT_RANGE
        # trace of (low, range) after each symbol for stepwise comparison
        self.trace: List[Tuple[int, int]] = []

    def _shift_low(self):
        low32 = self.low & MASK32
        carry = (self.low >> 32) & 1
        if low32 < 0xFF000000 or carry:
            self.out.append((self.cache + carry) & 0xFF)
            follower = (0xFF + carry) & 0xFF
            self.out.extend(bytes([follower]) * (self.cache_size - 1))
            self.cache = (low32 >> 24) & 0xFF
            self.cache_size = 0
        self.cache_size += 1
        self.low = (low32 << 8) & MASK32

    def encode(self, table: FreqTable, symbol: int):
        if not (0 <= symbol < len(table.freqs)):
            raise EncodeError("symbol out of range")
        cum, nxt = table.interval(symbol)
        freq = nxt - cum
        if freq == 0:
            raise EncodeError("zero frequency symbol")
        r = self.range // table.total
        self.low += r * cum
        self.range = r * freq
        while self.range < TOP:
            self.range <<= 8
            self._shift_low()
        self.trace.append((self.low, self.range))

    def finish(self) -> bytes:
        for _ in range(5):
            self._shift_low()
        return bytes(self.out)


class DecodeError(ValueError):
    pass


class RangeDecoder:
    def __init__(self, payload: bytes):
        if len(payload) < 5:
            raise DecodeError("truncated at seed")
        if payload[0] != 0:
            raise DecodeError(f"bad leading byte {payload[0]:#x}")
        self.buf = payload
        self.pos = 1
        self.code = int.from_bytes(payload[1:5], "big")
        self.pos = 5
        self.range = INIT_RANGE
        self.trace = []

    def _read(self) -> int:
        if self.pos >= len(self.buf):
            raise DecodeError(f"truncated at {self.pos}")
        b = self.buf[self.pos]
        self.pos += 1
        return b

    def decode(self, table: FreqTable) -> int:
        r = self.range // table.total
        value = self.code // r
        if value >= table.total:
            raise DecodeError(f"point {value} outside envelope total {table.total}")
        symbol = table.symbol_for(value)
        cum, nxt = table.interval(symbol)
        if nxt <= value:
            raise DecodeError("zero-width slot")
        self.code -= r * cum
        if self.code < 0:
            raise DecodeError("code below cum*r")
        self.range = r * (nxt - cum)
        while self.range < TOP:
            self.range <<= 8
            self.code = ((self.code << 8) | self._read()) & MASK32
        self.trace.append((self.code, self.range))
        return symbol

    def finish_check(self):
        if self.pos != len(self.buf):
            raise DecodeError(f"{len(self.buf) - self.pos} trailing bytes")


def encode_vec(freqs, bound, symbols):
    t = FreqTable(freqs, bound)
    e = RangeEncoder()
    for s in symbols:
        e.encode(t, s)
    return e.finish(), e.trace


def decode_vec(freqs, bound, payload, count):
    t = FreqTable(freqs, bound)
    d = RangeDecoder(payload)
    out = []
    for _ in range(count):
        out.append(d.decode(t))
    d.finish_check()
    return out, d.trace


if __name__ == "__main__":
    # quick self-check
    p, _ = encode_vec([1, 1], 1 << 16, [1])
    assert p == bytes([0x00, 0x7F, 0xFF, 0xFF, 0xFF]), p.hex()
    p0, _ = encode_vec([1, 1], 1 << 16, [])
    assert p0 == bytes(5)
    print("reference self-check ok")
