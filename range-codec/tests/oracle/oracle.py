#!/usr/bin/env python3
"""Independent reference implementation of the range-codec kernel and the
RC01 container, plus golden-vector generator for the Rust test-suite.

This file is NOT tested code and does not import the Rust implementation.
The Rust integration tests (tests/golden.rs) consume its output and assert
exact bytes / per-step interval state / failure categories. If this oracle
and the Rust kernel disagree, that is a real discrepancy to investigate,
not a test to relax.

Integer specification (mirrors src/range.rs, documented in FORMAT.md):

    TOP   = 2^24
    state: low (33-bit max), range (always >= TOP just before a symbol)
    before each symbol, while range < TOP:
        val   = low >> 8*3          # 0..=0x1FF
        carry = val >> 8           # 0 or 1
        emit  val & 0xFF
        if carry: walk emitted 0xFFs back to the sentinel, increment first
        low   = (low << 8) & 0xFFFFFFFF
        range <<= 8
    step  = range // total
    low  += step * cum[sym]
    range = step * freq[sym]
    finish: 4 shift-low steps (range untouched)

    decoder preamble: sentinel byte s in {0,1}, then 4 big-endian code bytes
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import zlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

TOP = 1 << 24
MAX_FREQ_TOTAL = 1 << 14
MAX_SYMBOLS = 256
MAX_CHUNK_SYMBOLS = 65_535
TAIL_SHIFTS = 4

MAGIC = b"RC01"
VERSION = 1
TAG_DATA = 0xD0
TAG_END = 0x45
FLAG_ADAPTIVE = 0x01


# ---------------------------------------------------------------- errors

class OracleError(Exception):
    """Label matches CodecError::code() in the Rust implementation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- kernel

@dataclass
class EncStep:
    index: int
    symbol: int
    cum: int
    freq: int
    low: int
    range: int
    emitted: List[int]


@dataclass
class DecStep:
    index: int
    code: int
    range: int
    scaled: int
    symbol: int


class RangeEncoder:
    def __init__(self, output_limit: Optional[int] = None):
        self.low = 0
        self.range = TOP
        self.out = bytearray([0x00])  # carry sentinel
        self.limit = output_limit
        self.trace: List[EncStep] = []

    def _guard(self, extra: int = 1):
        if self.limit is not None and len(self.out) + extra > self.limit:
            raise OracleError("OUTPUT_LIMIT_EXCEEDED", "output limit exceeded")

    def _shift_low(self):
        self._guard()
        val = self.low >> 24
        carry = val >> 8
        byte = val & 0xFF
        assert carry in (0, 1), "carry must be a single integer bit"
        self.out.append(byte)
        if carry:
            i = len(self.out) - 2
            while i >= 1 and self.out[i] == 0xFF:
                self.out[i] = 0x00
                i -= 1
            if self.out[i] + carry > 0xFF:
                raise OracleError("CARRY_OVERFLOW", "carry overflow past sentinel")
            self.out[i] += carry
        self.low = (self.low << 8) & 0xFFFFFFFF

    def _normalize(self) -> List[int]:
        emitted: List[int] = []
        while self.range < TOP:
            self._shift_low()
            emitted.append(self.out[-1])
            self.range <<= 8
        return emitted

    def encode_symbol(self, freqs: List[int], cum: List[int], total: int, sym: int):
        if not (0 <= sym < len(freqs)):
            raise OracleError("SYMBOL_OUT_OF_RANGE", f"symbol {sym}")
        if freqs[sym] == 0:
            raise OracleError("ZERO_FREQUENCY_SYMBOL", f"symbol {sym} has zero frequency")
        emitted = self._normalize()
        step = self.range // total
        self.low += step * cum[sym]
        self.range = step * freqs[sym]
        assert self.low < 1 << 33, "low exceeded the 33-bit integer bound"
        self.trace.append(
            EncStep(len(self.trace), sym, cum[sym], freqs[sym], self.low, self.range, emitted)
        )

    def finish(self) -> bytes:
        for _ in range(TAIL_SHIFTS):
            self._shift_low()
        return bytes(self.out)


class RangeDecoder:
    def __init__(self, data: bytes):
        if len(data) < 5:
            raise OracleError(
                "TRUNCATED", f"need 5 preamble bytes, have {len(data)}"
            )
        if data[0] > 1:
            raise OracleError("INVALID_INIT_BYTE", f"init byte {data[0]:#04x}")
        self.code = int.from_bytes(data[1:5], "big")
        self.range = TOP
        self.data = data
        self.pos = 5
        self.trace: List[DecStep] = []

    def _read(self) -> int:
        if self.pos >= len(self.data):
            raise OracleError("TRUNCATED", "ran out of renorm bytes")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def _normalize(self):
        while self.range < TOP:
            self.code = ((self.code << 8) | self._read()) & 0xFFFFFFFF
            self.range <<= 8

    def decode_symbol(self, freqs: List[int], cum: List[int], total: int) -> int:
        self._normalize()
        step = self.range // total
        scaled = self.code // step
        if scaled >= total:
            raise OracleError("CODE_OUTSIDE_RANGE", f"scaled {scaled} >= {total}")
        # containing symbol: last i with cum[i] <= scaled
        sym = 0
        for i in range(len(freqs)):
            if cum[i] <= scaled:
                sym = i
        if freqs[sym] == 0:
            raise OracleError("CODE_OUTSIDE_RANGE", "landed in a zero-width slot")
        self.code = self.code - step * cum[sym]
        self.range = step * freqs[sym]
        self.trace.append(DecStep(len(self.trace), self.code, self.range, scaled, sym))
        return sym


def cumulative(freqs: List[int]) -> Tuple[List[int], int]:
    if not (1 <= len(freqs) <= MAX_SYMBOLS):
        raise OracleError("BAD_ALPHABET_SIZE", f"alphabet {len(freqs)}")
    total = 0
    cum = [0]
    for f in freqs:
        total += f
        if total > MAX_FREQ_TOTAL:
            raise OracleError(
                "FREQ_TOTAL_OUT_OF_BOUNDS", f"total {total} > {MAX_FREQ_TOTAL}"
            )
        cum.append(total)
    if total == 0:
        raise OracleError("FREQ_TOTAL_OUT_OF_BOUNDS", "total is zero")
    return cum, total


# ---------------------------------------------------------------- models

def static_coder_stream(freqs: List[int], symbols: List[int]) -> Tuple[bytes, list, list]:
    cum, total = cumulative(freqs)
    enc = RangeEncoder()
    for s in symbols:
        enc.encode_symbol(freqs, cum, total, s)
    payload = enc.finish()

    dec = RangeDecoder(payload)
    for _ in symbols:
        dec.decode_symbol(freqs, cum, total)
    return payload, enc.trace, dec.trace


class AdaptiveModel:
    """Uniform start at frequency 1; integer halving rescale."""

    def __init__(self, n: int):
        if not (1 <= n <= MAX_SYMBOLS):
            raise OracleError("BAD_ALPHABET_SIZE", f"alphabet {n}")
        self.n = n
        self.freqs = [1] * n

    def table(self):
        return cumulative(self.freqs)

    def update(self, sym: int):
        if not (0 <= sym < self.n):
            raise OracleError("SYMBOL_OUT_OF_RANGE", f"symbol {sym}")
        self.freqs[sym] += 1
        if sum(self.freqs) > MAX_FREQ_TOTAL:
            self.freqs = [max(1, f // 2) for f in self.freqs]


def adaptive_stream(n: int, symbols: List[int]) -> Tuple[bytes, list, list]:
    enc = RangeEncoder()
    m = AdaptiveModel(n)
    for s in symbols:
        cum, total = m.table()
        enc.encode_symbol(m.freqs, cum, total, s)
        m.update(s)
    payload = enc.finish()

    dec = RangeDecoder(payload)
    m2 = AdaptiveModel(n)
    for _ in symbols:
        cum, total = m2.table()
        dec.decode_symbol(m2.freqs, cum, total)
        m2.update(dec.trace[-1].symbol)
    return payload, enc.trace, dec.trace


# ---------------------------------------------------------------- container

def _u16(v: int) -> bytes:
    return v.to_bytes(2, "big")


def _u32(v: int) -> bytes:
    return v.to_bytes(4, "big")


def _u64(v: int) -> bytes:
    return v.to_bytes(8, "big")


def encode_container_static(num_symbols: int, chunks: List[Tuple[List[int], List[int]]]) -> bytes:
    out = bytearray()
    declared = sum(len(syms) for _, syms in chunks)
    out += MAGIC + bytes([VERSION, 0]) + _u16(num_symbols) + _u64(declared) + _u32(len(chunks))
    for freqs, symbols in chunks:
        payload, _, _ = static_coder_stream(freqs, symbols)
        head = bytes([TAG_DATA]) + _u32(len(symbols)) + _u32(len(payload))
        table = b"".join(_u32(f) for f in freqs)
        body = head + table + payload
        out += body + _u32(zlib.crc32(body) & 0xFFFFFFFF)
    out.append(TAG_END)
    return bytes(out)


def encode_container_adaptive(num_symbols: int, chunks: List[List[int]]) -> bytes:
    out = bytearray()
    declared = sum(len(c) for c in chunks)
    out += MAGIC + bytes([VERSION, FLAG_ADAPTIVE]) + _u16(num_symbols)
    out += _u64(declared) + _u32(len(chunks))
    for symbols in chunks:
        payload, _, _ = adaptive_stream(num_symbols, symbols)
        body = bytes([TAG_DATA]) + _u32(len(symbols)) + _u32(len(payload)) + payload
        out += body + _u32(zlib.crc32(body) & 0xFFFFFFFF)
    out.append(TAG_END)
    return bytes(out)


def decode_container(data: bytes, max_symbol_count: int = 1 << 24,
                     max_chunks: int = 4096, max_payload_bytes: int = 64 << 20):
    """Independent oracle decoder; returns (symbols, info)."""
    p = 0

    def take(n: int):
        nonlocal p
        if p + n > len(data):
            raise OracleError(
                "TRUNCATED", f"need {n} at {p}, have {len(data) - p}"
            )
        v = data[p:p + n]
        p += n
        return v

    if take(4) != MAGIC:
        raise OracleError("BAD_MAGIC", "magic mismatch")
    (version,) = take(1)
    if version != VERSION:
        raise OracleError("UNSUPPORTED_VERSION", f"version {version}")
    (flags,) = take(1)
    if flags & ~FLAG_ADAPTIVE:
        raise OracleError("RESERVED_FLAG", f"flags {flags:#04x}")
    adaptive = bool(flags & FLAG_ADAPTIVE)
    num_symbols = int.from_bytes(take(2), "big")
    declared = int.from_bytes(take(8), "big")
    num_chunks = int.from_bytes(take(4), "big")
    if declared > max_symbol_count:
        raise OracleError("LENGTH_BUDGET_EXCEEDED", str(declared))
    if num_chunks > max_chunks:
        raise OracleError("CHUNK_BUDGET_EXCEEDED", str(num_chunks))

    symbols: List[int] = []
    total_payload = 0
    for idx in range(num_chunks):
        crc_start = p
        (tag,) = take(1)
        if tag != TAG_DATA:
            raise OracleError("UNKNOWN_CHUNK_TYPE", f"{tag:#04x}")
        sym_count = int.from_bytes(take(4), "big")
        if not (1 <= sym_count <= MAX_CHUNK_SYMBOLS):
            raise OracleError("BAD_CHUNK_LENGTH", f"chunk {idx}: {sym_count}")
        payload_len = int.from_bytes(take(4), "big")
        total_payload += payload_len
        if total_payload > max_payload_bytes:
            raise OracleError("BYTE_BUDGET_EXCEEDED", str(total_payload))
        if adaptive:
            freqs = None
        else:
            freqs = [int.from_bytes(take(4), "big") for _ in range(num_symbols)]
            cumulative(freqs)  # validates
        payload = bytes(take(payload_len))
        stored_crc = int.from_bytes(take(4), "big")
        if zlib.crc32(data[crc_start:p - 4]) & 0xFFFFFFFF != stored_crc:
            raise OracleError("CRC_MISMATCH", f"chunk {idx}")
        dec = RangeDecoder(payload)
        if adaptive:
            model = AdaptiveModel(num_symbols)
            for _ in range(sym_count):
                f = model.freqs
                cum, total = model.table()
                s = dec.decode_symbol(f, cum, total)
                model.update(s)
                symbols.append(s)
        else:
            cum, total = cumulative(freqs)
            for _ in range(sym_count):
                symbols.append(dec.decode_symbol(freqs, cum, total))
    if len(symbols) != declared:
        raise OracleError("LENGTH_MISMATCH", f"{declared} != {len(symbols)}")
    (end_tag,) = take(1)
    if end_tag != TAG_END:
        raise OracleError("UNKNOWN_CHUNK_TYPE", f"{end_tag:#04x}")
    if p != len(data):
        raise OracleError("TRAILING_BYTES", f"{len(data) - p} extra")
    return symbols, {"chunks": num_chunks, "num_symbols": num_symbols,
                     "adaptive": adaptive}


# ---------------------------------------------------------------- golden

def enc_trace_json(tr: List[EncStep]):
    return [
        {"index": t.index, "symbol": t.symbol, "cum": t.cum, "freq": t.freq,
         "low": t.low, "range": t.range, "emitted": t.emitted}
        for t in tr
    ]


def dec_trace_json(tr: List[DecStep]):
    return [
        {"index": t.index, "code": t.code, "range": t.range,
         "scaled": t.scaled, "symbol": t.symbol}
        for t in tr
    ]


def case_static(name: str, freqs: List[int], symbols: List[int], chunk_size: int):
    payload, etr, dtr = static_coder_stream(freqs, symbols)
    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)] if symbols else []
    if chunks:
        container = encode_container_static(
            len(freqs), [(freqs, c) for c in chunks]
        )
    else:
        # empty container helper, inlined
        out = MAGIC + bytes([VERSION, 0]) + _u16(len(freqs)) + _u64(0) + _u32(0)
        out += bytes([TAG_END])
        container = bytes(out)
    return {
        "name": name,
        "mode": "static",
        "num_symbols": len(freqs),
        "freqs": freqs,
        "symbols_hex": bytes(symbols).hex(),
        "chunk_size": chunk_size,
        "payload_hex": payload.hex(),
        "encoder_trace": enc_trace_json(etr),
        "decoder_trace": dec_trace_json(dtr),
        "container_hex": container.hex(),
        "expected_len": len(symbols),
        "expected_chunks": len(chunks),
    }


def case_adaptive(name: str, n: int, symbols: List[int], chunk_size: int):
    payload, etr, dtr = adaptive_stream(n, symbols)
    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]
    container = encode_container_adaptive(n, chunks)
    return {
        "name": name,
        "mode": "adaptive",
        "num_symbols": n,
        "symbols_hex": bytes(symbols).hex(),
        "chunk_size": chunk_size,
        "payload_hex": payload.hex(),
        "encoder_trace": enc_trace_json(etr),
        "decoder_trace": dec_trace_json(dtr),
        "container_hex": container.hex(),
        "expected_len": len(symbols),
        "expected_chunks": len(chunks),
    }


def gen_cases():
    cases = []

    # ---- very short inputs ------------------------------------------------
    cases.append(case_static("empty_alphabet3", [2, 1, 3], [], 4096))
    for s in range(3):
        cases.append(case_static(f"single_symbol_{s}", [2, 1, 3], [s], 4096))
    cases.append(case_static("six_mixed_symbols", [2, 1, 3], [0, 2, 1, 2, 0, 2], 4096))

    # ---- deep carry propagation ------------------------------------------
    # Seven 0s then a 1 over equal frequencies forces a carry through a run
    # of preceding 0xFF bytes; exact payload 00 00 01 00 00 00.
    cases.append(case_static(
        "deep_carry_backfill", [1, 1], [0, 0, 0, 0, 0, 0, 0, 1], 4096
    ))

    # ---- long repeated ----------------------------------------------------
    cases.append(case_static("long_repeat_single_alphabet", [1], [0] * 5000, 4096))
    cases.append(case_static(
        "long_repeat_two_symbols", [7, 3], [0] * 5000, 1024
    ))

    # ---- alternating ------------------------------------------------------
    alt = [(0 if i % 2 == 0 else 2) for i in range(4096)]
    cases.append(case_static("alternating_zero_gap", [3, 0, 5], alt, 512))

    # ---- all symbols ------------------------------------------------------
    allsym = list(range(256))
    cases.append(case_static("all_symbols_once", [1] * 256, allsym, 100))
    # every symbol, repeated rounds, uniform frequencies
    rounds = (allsym * 8)
    cases.append(case_static("all_symbols_eight_rounds", [1] * 256, rounds, 4096))

    # ---- skewed frequencies forcing renorm/carry paths --------------------
    skewed = [(i * 37) % 256 for i in range(12_000)]
    freqs = [(i % 7) + 1 for i in range(256)]
    cases.append(case_static("skewed_12k", freqs, skewed, 2048))

    # ---- adaptive ---------------------------------------------------------
    ad_syms = [(i * 7 + (i >> 3)) % 4 for i in range(3000)]
    cases.append(case_adaptive("adaptive_small_alphabet_3k", 4, ad_syms, 333))
    ad2 = [(i * 31 + (i // 11)) % 256 for i in range(6000)]
    cases.append(case_adaptive("adaptive_full_alphabet_6k", 256, ad2, 1000))

    return cases


def gen_negatives(cases):
    """Negative fixtures with the error label assigned by THIS oracle."""
    negs = []

    # illegal frequency tables (encoder-side rejection)
    negs.append({
        "name": "zero_total_table",
        "kind": "encode_static",
        "freqs": [0, 0, 0],
        "symbols_hex": "",
        "expected_code": "FREQ_TOTAL_OUT_OF_BOUNDS",
    })
    negs.append({
        "name": "overbound_total",
        "kind": "encode_static",
        "freqs": [9000, 7000, 401],
        "symbols_hex": "",
        "expected_code": "FREQ_TOTAL_OUT_OF_BOUNDS",
    })
    negs.append({
        "name": "empty_alphabet",
        "kind": "encode_static",
        "freqs": [],
        "symbols_hex": "",
        "expected_code": "BAD_ALPHABET_SIZE",
    })
    negs.append({
        "name": "encode_zero_frequency_symbol",
        "kind": "encode_static",
        "freqs": [1, 0, 2],
        "symbols_hex": "01",
        "expected_code": "ZERO_FREQUENCY_SYMBOL",
    })
    negs.append({
        "name": "symbol_out_of_range",
        "kind": "encode_static",
        "freqs": [1, 1],
        "symbols_hex": "02",
        "expected_code": "SYMBOL_OUT_OF_RANGE",
    })

    # container truncations / corruption — use the skewed static case
    src = next(c for c in cases if c["name"] == "skewed_12k")
    blob = bytes.fromhex(src["container_hex"])

    # header truncation: magic+version only (5 bytes)
    negs.append({
        "name": "truncated_header",
        "kind": "decode",
        "blob_hex": blob[:5].hex(),
        "expected_code": "TRUNCATED",
    })
    # chop final END + most of last chunk bytes -> inner stream truncated
    negs.append({
        "name": "truncated_payload_inner",
        "kind": "decode",
        "blob_hex": blob[: len(blob) - 6].hex(),
        "expected_code": "TRUNCATED",
    })
    # flip a payload byte -> CRC must fail before kernel runs
    damaged = bytearray(blob)
    damaged[len(blob) - 8] ^= 0xA5
    negs.append({
        "name": "payload_bitflip_crc",
        "kind": "decode",
        "blob_hex": bytes(damaged).hex(),
        "expected_code": "CRC_MISMATCH",
    })
    # bad magic
    bad = bytearray(blob)
    bad[0] = ord("X")
    negs.append({
        "name": "bad_magic",
        "kind": "decode",
        "blob_hex": bytes(bad).hex(),
        "expected_code": "BAD_MAGIC",
    })
    # unsupported version
    bad = bytearray(blob)
    bad[4] = 99
    negs.append({
        "name": "bad_version",
        "kind": "decode",
        "blob_hex": bytes(bad).hex(),
        "expected_code": "UNSUPPORTED_VERSION",
    })
    # reserved flag set -> indeterminate
    bad = bytearray(blob)
    bad[5] = 0x80
    negs.append({
        "name": "reserved_flag",
        "kind": "decode",
        "blob_hex": bytes(bad).hex(),
        "expected_code": "RESERVED_FLAG",
        "expected_decision": "indeterminate",
    })
    # trailing bytes
    negs.append({
        "name": "trailing_bytes",
        "kind": "decode",
        "blob_hex": (blob + b"\x00").hex(),
        "expected_code": "TRAILING_BYTES",
    })
    # bad kernel init byte inside the payload (payload CRC recomputed so the
    # error is delivered by the kernel, not the CRC check)
    import struct
    bad = bytearray(blob)
    # header is 20 bytes; chunk prefix is tag(1)+cnt(4)+plen(4)=9;
    # static table follows (256 u32s = 1024 bytes), then payload begins.
    header_len = 4 + 1 + 1 + 2 + 8 + 4  # = 20
    payload_start = header_len + 1 + 4 + 4 + 256 * 4
    bad[payload_start] = 0x7F
    plen = int.from_bytes(bad[25:29], "big")
    body_end = payload_start + plen
    body = bytes(bad[header_len:body_end])
    crc = zlib.crc32(body) & 0xFFFFFFFF
    bad[body_end:body_end + 4] = struct.pack(">I", crc)
    negs.append({
        "name": "kernel_bad_init_byte",
        "kind": "decode",
        "blob_hex": bytes(bad).hex(),
        "expected_code": "INVALID_INIT_BYTE",
    })

    return negs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="golden JSON path")
    ap.add_argument("--decode", default=None, help="decode a container file and print JSON")
    args = ap.parse_args()

    if args.decode:
        with open(args.decode, "rb") as fh:
            data = fh.read()
        try:
            syms, info = decode_container(data)
            print(json.dumps({"ok": True, "symbols_hex": bytes(syms).hex(), **info}))
        except OracleError as e:
            print(json.dumps({"ok": False, "code": e.code, "message": str(e)}))
        return

    cases = gen_cases()

    # Self-check: oracle decoder must reproduce every encoded case.
    for c in cases:
        syms, info = decode_container(bytes.fromhex(c["container_hex"]))
        assert bytes(syms).hex() == c["symbols_hex"], c["name"]
        assert info["chunks"] == c["expected_chunks"], c["name"]

    doc = {
        "spec": {
            "TOP": TOP,
            "MAX_FREQ_TOTAL": MAX_FREQ_TOTAL,
            "MAX_SYMBOLS": MAX_SYMBOLS,
            "TAIL_SHIFTS": TAIL_SHIFTS,
            "byte_order": "big-endian",
        },
        "cases": cases,
        "negatives": gen_negatives(cases),
    }
    out_path = args.out or os.path.join(
        os.path.dirname(__file__), "..", "..", "fixtures", "golden", "golden.json"
    )
    out_path = os.path.abspath(out_path)
    with open(out_path, "w") as fh:
        json.dump(doc, fh, indent=1)
    print(f"wrote {len(cases)} cases and {len(doc['negatives'])} negatives -> {out_path}")


if __name__ == "__main__":
    main()
