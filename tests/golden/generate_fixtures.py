#!/usr/bin/env python3
"""
Independent reference oracle and golden-vector generator for the HCMP
canonical-Huffman container.

This file is an *independent* implementation of the format specification in
README.md / FORMAT.md:

  * Huffman tree construction uses a priority queue keyed on
    (weight, height, min_symbol);
  * code words are derived canonically, sorted (length, symbol), MSB-first;
  * code-length tables use the documented literal/repeat opcode stream;
  * blocks pack an RLE table, an 8-byte bit_total and an MSB-first body;
  * containers carry a 16-byte global header, a CRC-protected directory of
    32-byte entries and contiguous payloads.

It shares no code with the Rust implementation under test. Its outputs are
consumed by tests/golden.rs, where an independently-written Rust decoder
(not using the crate's own decoder) verifies them byte-for-byte.

Usage:
    python3 tests/golden/generate_fixtures.py [output_dir]

If output_dir is omitted, vectors are written next to this script.

The script is deterministic: every random vector uses a fixed seed.
"""

from __future__ import annotations

import json
import os
import random
import struct
import sys
import zlib
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence, Tuple

MAGIC = b"HCMP"
VERSION = 1
GLOBAL_HEADER_LEN = 16
DIR_ENTRY_LEN = 32
MAX_CODE_BITS = 32

# RLE opcode bounds (must match FORMAT.md).
OP_LITERAL_MAX = 0xC8           # 200
OP_REPEAT_BASE = 0xC8          # repeat extra-copies = op - 0xC8
MAX_REPEAT_EXTRA = 55


# --------------------------------------------------------------------------- #
# Huffman kernel
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, order=True)
class HeapKey:
    weight: int
    height: int
    min_symbol: int


class Node:
    __slots__ = ("left", "right")

    def __init__(self, left: "Node", right: "Node") -> None:
        self.left = left
        self.right = right


class Leaf(Node):
    __slots__ = ("symbol",)

    def __init__(self, symbol: int) -> None:
        super().__init__(None, None)  # type: ignore[arg-type]
        self.symbol = symbol


def build_lengths(freq: Sequence[int]) -> List[Tuple[int, int]]:
    """Return (symbol, length) sorted by (length, symbol).

    Implements the normative tie-break: pop order by
    (weight, height, min_symbol).
    """
    present = [s for s, f in enumerate(freq) if f > 0]
    if not present:
        return []
    if len(present) == 1:
        return [(present[0], 0)]

    # Parallel arrays act as the node arena.
    nodes: List[Node] = []
    weights: List[int] = []
    heights: List[int] = []
    mins: List[int] = []
    heap: List[Tuple[HeapKey, int]] = []

    def push(idx: int) -> None:
        heap.append((HeapKey(weights[idx], heights[idx], mins[idx]), idx))
        heap.sort(key=lambda kv: kv[0])  # tiny n (<=511); clarity over speed

    for s in present:
        idx = len(nodes)
        nodes.append(Leaf(s))
        weights.append(freq[s])
        heights.append(0)
        mins.append(s)
        push(idx)

    while len(heap) > 1:
        _, a = heap.pop(0)
        _, b = heap.pop(0)
        idx = len(nodes)
        nodes.append(Node(a, b))
        weights.append(weights[a] + weights[b])
        heights.append(1 + max(heights[a], heights[b]))
        mins.append(min(mins[a], mins[b]))
        push(idx)

    root = heap[0][1]

    depths: Dict[int, int] = {}
    stack: List[Tuple[int, int]] = [(root, 0)]
    while stack:
        idx, depth = stack.pop()
        node = nodes[idx]
        if isinstance(node, Leaf):
            depths[node.symbol] = depth
        else:
            stack.append((node.left, depth + 1))
            stack.append((node.right, depth + 1))

    table = [(s, depths[s]) for s in present]
    table.sort(key=lambda sl: (sl[1], sl[0]))
    max_len = max((l for _, l in table), default=0)
    if max_len > MAX_CODE_BITS:
        raise ValueError(f"code length {max_len} exceeds {MAX_CODE_BITS}")
    return table


def canonical_codes(table: Sequence[Tuple[int, int]]) -> List[int]:
    """Integer code values (not MSB-aligned), standard recurrence."""
    if not table:
        return []
    if table[0][1] == 0:
        assert len(table) == 1
        return [0]
    codes: List[int] = []
    code = 0
    prev = table[0][1]
    for i, (_, length) in enumerate(table):
        if i > 0:
            code = (code + 1) << (length - prev)
        codes.append(code)
        prev = length
    return codes


# --------------------------------------------------------------------------- #
# Table RLE and block body
# --------------------------------------------------------------------------- #

def encode_table(table: Sequence[Tuple[int, int]]) -> bytes:
    n = len(table)
    out = bytearray()
    out += struct.pack(">H", n)
    if n == 0:
        return bytes(out)
    if n == 1:
        sym, length = table[0]
        assert length == 0
        out.append(sym)
        return bytes(out)

    by_sym = [0] * 256
    for sym, length in table:
        assert 1 <= length <= MAX_CODE_BITS
        by_sym[sym] = length
    present = [s for s in range(256) if by_sym[s] > 0]
    first, last = present[0], present[-1]
    out.append(first)
    out.append(last)

    span = by_sym[first:last + 1]
    i = 0
    while i < len(span):
        run = 1
        while i + run < len(span) and span[i + run] == span[i]:
            run += 1
        if run >= 2:
            out.append(1)
            out.append(span[i])
            remaining = run - 1
            while remaining > 0:
                take = min(remaining, MAX_REPEAT_EXTRA)
                out.append(OP_REPEAT_BASE + take)
                remaining -= take
            i += run
        else:
            start = i
            while i < len(span) and (i - start) < OP_LITERAL_MAX:
                r = 1
                while i + r < len(span) and span[i + r] == span[i]:
                    r += 1
                if r >= 2:
                    break
                i += 1
            group = span[start:i]
            assert 1 <= len(group) <= OP_LITERAL_MAX
            out.append(len(group))
            out.extend(group)
    return bytes(out)


def encode_block(data: bytes) -> bytes:
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    table = build_lengths(freq)
    payload = bytearray(encode_table(table))

    if len(table) >= 2:
        codes = canonical_codes(table)
        code_of = {sym: code for (sym, _), code in zip(table, codes)}
        len_of = {sym: length for sym, length in table}
        bits = 0
        nbits = 0
        for b in data:
            length = len_of[b]
            bits = (bits << length) | code_of[b]
            nbits += length
        body_len = (nbits + 7) // 8
        if body_len:
            packed = bits << (body_len * 8 - nbits)
            body = packed.to_bytes(body_len, "big")
        else:
            body = b""
    else:
        nbits = 0
        body = b""

    payload += struct.pack(">Q", nbits)
    payload += body
    return bytes(payload)


@dataclass
class Entry:
    block_id: int
    original_len: int
    payload_off: int
    payload_len: int
    payload_crc32: int
    original_crc32: int

    def wire(self) -> bytes:
        return (
            struct.pack(">I", self.block_id)
            + struct.pack(">Q", self.original_len)
            + struct.pack(">Q", self.payload_off)
            + struct.pack(">I", self.payload_len)
            + struct.pack(">I", self.payload_crc32)
            + struct.pack(">I", self.original_crc32)
        )


def encode_container(chunks: Sequence[bytes]) -> bytes:
    encoded = [encode_block(c) for c in chunks]
    count = len(encoded)
    cursor = GLOBAL_HEADER_LEN + DIR_ENTRY_LEN * count
    entries: List[Entry] = []
    for i, payload in enumerate(encoded):
        off = cursor
        cursor += len(payload)
        entries.append(
            Entry(
                block_id=i,
                original_len=len(chunks[i]),
                payload_off=off,
                payload_len=len(payload),
                payload_crc32=zlib.crc32(payload) & 0xFFFFFFFF,
                original_crc32=zlib.crc32(chunks[i]) & 0xFFFFFFFF,
            )
        )
    directory = b"".join(e.wire() for e in entries)
    out = bytearray()
    out += MAGIC
    out.append(VERSION)
    out.append(0)            # flags
    out += b"\x00\x00"      # reserved
    out += struct.pack(">I", count)
    out += struct.pack(">I", zlib.crc32(directory) & 0xFFFFFFFF)
    out += directory
    for payload in encoded:
        out += payload
    return bytes(out)


def frequencies(data: bytes) -> List[int]:
    f = [0] * 256
    for b in data:
        f[b] += 1
    return f


def table_prefix_len(payload: bytes) -> int:
    """Byte offset of the 8-byte bit_total field in an encoded block."""
    num = struct.unpack(">H", payload[0:2])[0]
    p = 2
    if num == 0:
        return p
    if num == 1:
        return p + 1
    first, last = payload[p], payload[p + 1]
    p += 2
    span = last - first + 1
    emitted = 0
    while emitted < span:
        op = payload[p]
        p += 1
        if op <= OP_LITERAL_MAX:
            p += op
            emitted += op
        else:
            emitted += op - OP_REPEAT_BASE
    return p


# --------------------------------------------------------------------------- #
# Independent decoder (different shape than the Rust crate: byte-aligned
# dictionary built directly, then a streaming bit-walk).
# --------------------------------------------------------------------------- #

def independent_decode_container(blob: bytes) -> List[bytes]:
    if len(blob) < GLOBAL_HEADER_LEN:
        raise ValueError("truncated_header")
    if blob[0:4] != MAGIC:
        raise ValueError("bad_magic")
    if blob[4] != VERSION:
        raise ValueError("unknown_version")
    if blob[5] != 0 or blob[6:8] != b"\x00\x00":
        raise ValueError("bad_flags")
    count = struct.unpack(">I", blob[8:12])[0]
    dir_crc = struct.unpack(">I", blob[12:16])[0]
    dir_end = GLOBAL_HEADER_LEN + DIR_ENTRY_LEN * count
    directory = blob[GLOBAL_HEADER_LEN:dir_end]
    if zlib.crc32(directory) & 0xFFFFFFFF != dir_crc:
        raise ValueError("directory_crc_mismatch")

    results: List[bytes] = []
    expected_off = dir_end
    for i in range(count):
        raw = directory[i * DIR_ENTRY_LEN:(i + 1) * DIR_ENTRY_LEN]
        block_id = struct.unpack(">I", raw[0:4])[0]
        olen = struct.unpack(">Q", raw[4:12])[0]
        off = struct.unpack(">Q", raw[12:20])[0]
        plen = struct.unpack(">I", raw[20:24])[0]
        pcrc = struct.unpack(">I", raw[24:28])[0]
        ocrc = struct.unpack(">I", raw[28:32])[0]
        if off != expected_off:
            raise ValueError("payload_overlap")
        end = off + plen
        payload = blob[off:end]
        if zlib.crc32(payload) & 0xFFFFFFFF != pcrc:
            raise ValueError("payload_crc_mismatch")
        decoded = independent_decode_block(payload, olen)
        if zlib.crc32(decoded) & 0xFFFFFFFF != ocrc or len(decoded) != olen:
            raise ValueError("original_crc_mismatch")
        results.append(decoded)
        expected_off = end
    if expected_off != len(blob):
        raise ValueError("trailing_garbage")
    return results


def independent_decode_block(payload: bytes, expected_len: int) -> bytes:
    pos = 0
    num = struct.unpack(">H", payload[pos:pos + 2])[0]
    pos += 2
    if num == 0:
        bit_total = struct.unpack(">Q", payload[pos:pos + 8])[0]
        assert bit_total == 0 and expected_len == 0
        return b""
    if num == 1:
        sym = payload[pos]
        pos += 1
        bit_total = struct.unpack(">Q", payload[pos:pos + 8])[0]
        assert bit_total == 0
        return bytes([sym]) * expected_len

    first = payload[pos]
    last = payload[pos + 1]
    pos += 2
    span_len = last - first + 1
    lengths = [0] * 256
    emitted = 0
    prev = None
    while emitted < span_len:
        op = payload[pos]
        pos += 1
        if op == 0:
            raise ValueError("reserved_rle_opcode")
        if op <= OP_LITERAL_MAX:
            for _ in range(op):
                lit = payload[pos]
                pos += 1
                if lit > MAX_CODE_BITS:
                    raise ValueError("code_length_too_long")
                lengths[first + emitted] = lit
                emitted += 1
                prev = lit
        else:
            repeat = op - OP_REPEAT_BASE
            if repeat == 0 or prev is None:
                raise ValueError("invalid_rle_run")
            for _ in range(repeat):
                lengths[first + emitted] = prev
                emitted += 1
    present = [(s, lengths[s]) for s in range(first, last + 1) if lengths[s] > 0]
    if len(present) != num:
        raise ValueError("duplicate_symbol")
    # Kraft equality.
    maxl = max(l for _, l in present)
    kraft = sum(1 << (maxl - l) for _, l in present)
    if kraft != 1 << maxl:
        raise ValueError("over_subscribed_tree")

    bit_total = struct.unpack(">Q", payload[pos:pos + 8])[0]
    pos += 8
    body = payload[pos:]
    if len(body) != (bit_total + 7) // 8:
        raise ValueError("truncated_bitstream")

    codes = canonical_codes(sorted(present, key=lambda sl: (sl[1], sl[0])))
    # Build (length, integer code) -> symbol map.
    mapping = {}
    for (sym, length), code in zip(sorted(present, key=lambda sl: (sl[1], sl[0])), codes):
        mapping[(length, code)] = sym

    out = bytearray()
    acc = 0
    depth = 0
    for bit_index in range(bit_total):
        byte = body[bit_index // 8]
        bit = (byte >> (7 - (bit_index % 8))) & 1
        acc = (acc << 1) | bit
        depth += 1
        if (depth, acc) in mapping:
            out.append(mapping[(depth, acc)])
            acc = 0
            depth = 0
    if depth != 0:
        raise ValueError("truncated_bitstream")
    if len(out) != expected_len:
        raise ValueError("length_mismatch")
    # Padding bits must be zero.
    total = len(body) * 8
    for bit_index in range(bit_total, total):
        byte = body[bit_index // 8]
        if (byte >> (7 - (bit_index % 8))) & 1:
            raise ValueError("invalid_trailing_bits")
    return bytes(out)


# --------------------------------------------------------------------------- #
# Golden vectors
# --------------------------------------------------------------------------- #

def hexlify(b: bytes) -> str:
    return b.hex()


def write_vector(out_dir: str, name: str, blob: bytes, meta: dict) -> str:
    path = os.path.join(out_dir, f"{name}.bin")
    with open(path, "wb") as f:
        f.write(blob)
    meta = dict(meta)
    meta["file"] = f"{name}.bin"
    meta["sha256"] = __import__("hashlib").sha256(blob).hexdigest()
    meta["length_bytes"] = len(blob)
    return name


def write_original(out_dir: str, name: str, original: bytes) -> str:
    """Write the independent ground-truth original (concatenated blocks)."""
    fname = f"{name}.orig.bin"
    with open(os.path.join(out_dir, fname), "wb") as f:
        f.write(original)
    return fname


def make_length_vector() -> Tuple[str, bytes, dict]:
    # Tie-break-discriminating tables plus the classic profile.
    cases = []

    def freqs(pairs: List[Tuple[int, int]]) -> List[int]:
        f = [0] * 256
        for s, c in pairs:
            f[s] = c
        return f

    profiles = {
        "one_one_one_two": ([(0, 1), (1, 1), (2, 1), (3, 2)],
                            [(0, 2), (1, 2), (2, 2), (3, 2)]),
        "four_equal": ([(0, 1), (1, 1), (2, 1), (3, 1)],
                       [(0, 2), (1, 2), (2, 2), (3, 2)]),
        "classic_45": ([(0, 45), (1, 13), (2, 12), (3, 16), (4, 9), (5, 5)],
                       [(0, 1), (1, 3), (2, 3), (3, 3), (4, 4), (5, 4)]),
        "height_before_symbol": (
            [(0, 1), (1, 2), (2, 3), (3, 3), (4, 3)],
            # 0+1=3 (height1); weight-3 leaves (height0) pop before the
            # internal (height1), so they pair together:
            [(2, 2), (3, 2), (4, 2), (0, 3), (1, 3)],
        ),
    }
    for name, (pairs, expected) in profiles.items():
        table = build_lengths(freqs(pairs))
        assert [(s, l) for s, l in table] == expected, (name, table)
        codes_int = canonical_codes(table)
        cases.append({
            "name": name,
            "freq": pairs,
            "lengths": [[s, l] for s, l in table],
            "codes": [[s, l, format(c, f"0{l}b") if l else ""] for (s, l), c in zip(table, codes_int)],
        })
    meta = {"kind": "lengths", "cases": cases}
    blob = (json.dumps(meta, sort_keys=True) + "\n").encode()
    return "lengths", blob, meta


def main() -> int:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out_dir, exist_ok=True)

    manifest: List[dict] = []
    generated_by = "tests/golden/generate_fixtures.py (independent Python oracle)"

    # 1) Exact code-length / tie-break vectors (JSON).
    name, blob, meta = make_length_vector()
    path = os.path.join(out_dir, f"{name}.json")
    with open(path, "w") as f:
        f.write(blob.decode())
    manifest.append({"name": name, "file": f"{name}.json", "kind": "lengths",
                     "cases": len(meta["cases"])})

    # 2) Empty input (single empty block).
    v = encode_container([b""])
    assert independent_decode_container(v) == [b""]
    write_vector(out_dir, "empty", v,
                 {"kind": "roundtrip", "blocks": [{"original_len": 0}],
                  "generated_by": generated_by})
    of = write_original(out_dir, "empty", b"")
    manifest.append({"name": "empty", "file": "empty.bin", "original_file": of,
                     "kind": "roundtrip", "blocks": 1})

    # 3) Lone symbol, various lengths.
    for n in (1, 7, 1000):
        data = b"z" * n
        v = encode_container([data])
        assert independent_decode_container(v) == [data]
        write_vector(out_dir, f"single_{n}", v,
                     {"kind": "roundtrip",
                      "blocks": [{"original_len": n}], "generated_by": generated_by})
        of = write_original(out_dir, f"single_{n}", data)
        manifest.append({"name": f"single_{n}", "file": f"single_{n}.bin",
                         "original_file": of, "kind": "roundtrip", "blocks": 1})

    # 4) All 256 byte values.
    data = bytes(range(256))
    v = encode_container([data])
    assert independent_decode_container(v) == [data]
    write_vector(out_dir, "all_bytes", v,
                 {"kind": "roundtrip",
                  "blocks": [{"original_len": 256}], "generated_by": generated_by})
    of = write_original(out_dir, "all_bytes", data)
    manifest.append({"name": "all_bytes", "file": "all_bytes.bin",
                     "original_file": of, "kind": "roundtrip", "blocks": 1})

    # 5) Extreme skew: one dominant symbol + rare symbols, multi-block split.
    rng = random.Random(0x4843_4D50)  # "HCMP"
    arr = bytearray([0xAB] * 20000)
    arr.extend(rng.randrange(256) for _ in range(64))
    rng.shuffle(arr)
    skew = bytes(arr)
    chunks = [skew[i:i + 4096] for i in range(0, len(skew), 4096)]
    v = encode_container(chunks)
    assert independent_decode_container(v) == chunks
    write_vector(out_dir, "extreme_skew", v,
                 {"kind": "roundtrip",
                  "blocks": [{"original_len": len(c)} for c in chunks],
                  "seed": "0x48434D50", "generated_by": generated_by})
    of = write_original(out_dir, "extreme_skew", b"".join(chunks))
    manifest.append({"name": "extreme_skew", "file": "extreme_skew.bin",
                     "original_file": of, "kind": "roundtrip", "blocks": len(chunks)})

    # 6) Pseudo-random data, fixed seed.
    rng = random.Random(20260927)
    rand = bytes(rng.randrange(256) for _ in range(12000))
    chunks = [rand[i:i + 4096] for i in range(0, len(rand), 4096)]
    v = encode_container(chunks)
    assert independent_decode_container(v) == chunks
    write_vector(out_dir, "random_12k", v,
                 {"kind": "roundtrip",
                  "blocks": [{"original_len": len(c)} for c in chunks],
                  "seed": "20260927", "generated_by": generated_by})
    of = write_original(out_dir, "random_12k", b"".join(chunks))
    manifest.append({"name": "random_12k", "file": "random_12k.bin",
                     "original_file": of, "kind": "roundtrip",
                     "blocks": len(chunks)})

    # 7) Text sample (exercises interior gaps in the symbol span).
    text = (b"the quick brown fox jumps over the lazy dog. " * 32)
    v = encode_container([text])
    assert independent_decode_container(v) == [text]
    write_vector(out_dir, "text", v,
                 {"kind": "roundtrip",
                  "blocks": [{"original_len": len(text)}], "generated_by": generated_by})
    of = write_original(out_dir, "text", text)
    manifest.append({"name": "text", "file": "text.bin", "original_file": of,
                     "kind": "roundtrip", "blocks": 1})

    # ------------------------------------------------------------------ #
    # Malformed vectors — the exact failure classes tested in golden.rs.
    # ------------------------------------------------------------------ #

    base_chunks = [b"abracadabra", b"hello huffman world"]
    base = bytearray(encode_container(base_chunks))

    def recompute_directory(blob: bytearray) -> None:
        count = struct.unpack(">I", blob[8:12])[0]
        d0 = GLOBAL_HEADER_LEN
        d1 = d0 + DIR_ENTRY_LEN * count
        blob[12:16] = struct.pack(">I", zlib.crc32(bytes(blob[d0:d1])) & 0xFFFFFFFF)

    # 7a) Unknown container version.
    bad = bytearray(base)
    bad[4] = VERSION + 99
    write_vector(out_dir, "malformed_unknown_version", bytes(bad),
                 {"kind": "malformed", "expect_error": "unknown_version",
                  "generated_by": generated_by})
    manifest.append({"name": "malformed_unknown_version",
                     "file": "malformed_unknown_version.bin", "kind": "malformed",
                     "expect_error": "unknown_version"})

    # 7b) Over-subscribed code tree (three length-1 symbols) inside block 0.
    # Build one block payload by hand: num_sym=3, span 0..=2, then three
    # literal length bytes each equal to 1, bit_total=0 and empty body.
    table = bytearray()
    table += struct.pack(">H", 3)
    table += bytes([0, 2])
    table += bytes([3, 1, 1, 1])  # literal op count=3, lengths 1,1,1
    table += struct.pack(">Q", 0)
    payload0 = bytes(table)
    # Valid second block so directory layout is realistic.
    payload1 = encode_block(b"x")
    count = 2
    cursor = GLOBAL_HEADER_LEN + DIR_ENTRY_LEN * count
    e0 = Entry(0, 11, cursor, len(payload0),
               zlib.crc32(payload0) & 0xFFFFFFFF,
               zlib.crc32(b"abracadabra") & 0xFFFFFFFF).wire()
    e1 = Entry(1, 1, cursor + len(payload0), len(payload1),
               zlib.crc32(payload1) & 0xFFFFFFFF,
               zlib.crc32(b"x") & 0xFFFFFFFF).wire()
    directory = e0 + e1
    blob = bytearray()
    blob += MAGIC + bytes([VERSION, 0]) + b"\x00\x00"
    blob += struct.pack(">I", count)
    blob += struct.pack(">I", zlib.crc32(directory) & 0xFFFFFFFF)
    blob += directory + payload0 + payload1
    write_vector(out_dir, "malformed_over_subscribed", bytes(blob),
                 {"kind": "malformed", "expect_error": "over_subscribed_tree",
                  "generated_by": generated_by})
    manifest.append({"name": "malformed_over_subscribed",
                     "file": "malformed_over_subscribed.bin", "kind": "malformed",
                     "expect_error": "over_subscribed_tree"})

    # 7c) Truncated code word. With three symbols "ababc" (freq a=2,b=2,c=1)
    # the optimal table assigns the rare symbol c a 2-bit code (a,b take
    # length 2 and length 1), so the stream ends with a multi-bit code.
    # Cutting bit_total down by one strips exactly the final bit of the last
    # c code — the stream ends mid-code (depth 1), which every conforming
    # decoder must classify as truncated_bitstream.
    trunc_original = b"ababc"
    good_payload = bytearray(encode_block(trunc_original))

    pfx = table_prefix_len(good_payload)
    orig_bits = struct.unpack(">Q", good_payload[pfx:pfx + 8])[0]
    assert orig_bits % 8 == 0 and orig_bits >= 8, f"got {orig_bits}"
    new_bits = orig_bits - 1
    # ceil is unchanged for a reduction of one when the stream is byte
    # aligned and the last code occupies >=2 bits; verify that explicitly.
    assert (new_bits + 7) // 8 == (orig_bits + 7) // 8
    hacked = bytearray(good_payload)
    hacked[pfx:pfx + 8] = struct.pack(">Q", new_bits)
    try:
        independent_decode_block(bytes(hacked), len(trunc_original))
        raise AssertionError("truncated fixture unexpectedly decoded")
    except ValueError as e:
        assert str(e) == "truncated_bitstream", e
    payload0 = bytes(hacked)
    payload1 = encode_block(b"x")
    count = 2
    cursor = GLOBAL_HEADER_LEN + DIR_ENTRY_LEN * count
    e0 = Entry(0, len(trunc_original), cursor, len(payload0),
               zlib.crc32(payload0) & 0xFFFFFFFF,
               zlib.crc32(trunc_original) & 0xFFFFFFFF).wire()
    e1 = Entry(1, 1, cursor + len(payload0), len(payload1),
               zlib.crc32(payload1) & 0xFFFFFFFF,
               zlib.crc32(b"x") & 0xFFFFFFFF).wire()
    directory = e0 + e1
    blob = bytearray()
    blob += MAGIC + bytes([VERSION, 0]) + b"\x00\x00"
    blob += struct.pack(">I", count)
    blob += struct.pack(">I", zlib.crc32(directory) & 0xFFFFFFFF)
    blob += directory + payload0 + payload1
    write_vector(out_dir, "malformed_truncated_code", bytes(blob),
                 {"kind": "malformed", "expect_error": "truncated_bitstream",
                  "note": "bit_total ends mid-code", "generated_by": generated_by})
    manifest.append({"name": "malformed_truncated_code",
                     "file": "malformed_truncated_code.bin", "kind": "malformed",
                     "expect_error": "truncated_bitstream"})

    # 7d) Bad trailing padding: take a valid one-block container and set the
    # first padding bit (immediately after bit_total) in its final byte, then
    # recompute the payload CRC in the directory.
    raw_block = encode_block(b"abracadabra")
    pfx = table_prefix_len(raw_block)
    orig_bits = struct.unpack(">Q", raw_block[pfx:pfx + 8])[0]
    # Choose data whose total bits leave at least one padding bit; the
    # encoder for this alphabet does, but guarantee it.
    assert orig_bits % 8 != 0, "fixture needs padding bits"
    v = bytearray(encode_container([b"abracadabra"]))
    body_start_file = GLOBAL_HEADER_LEN + DIR_ENTRY_LEN + pfx + 8
    pad_byte_index = body_start_file + orig_bits // 8
    pad_bit_within = orig_bits % 8  # 0..=7, first padding bit position
    v[pad_byte_index] |= 1 << (7 - pad_bit_within)
    count = 1
    d0 = GLOBAL_HEADER_LEN
    plen_field = d0 + 20
    payload_len = struct.unpack(">I", v[plen_field:plen_field + 4])[0]
    payload_start = d0 + DIR_ENTRY_LEN
    payload = bytes(v[payload_start:payload_start + payload_len])
    v[d0 + 24:d0 + 28] = struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
    recompute_directory(v)
    write_vector(out_dir, "malformed_bad_padding", bytes(v),
                 {"kind": "malformed", "expect_error": "invalid_trailing_bits",
                  "generated_by": generated_by})
    manifest.append({"name": "malformed_bad_padding",
                     "file": "malformed_bad_padding.bin", "kind": "malformed",
                     "expect_error": "invalid_trailing_bits"})

    # Manifest.
    manifest_doc = {
        "format": "HCMP",
        "version": VERSION,
        "generator": generated_by,
        "python": sys.version.split()[0],
        "vectors": manifest,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest_doc, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"wrote {len(manifest)} golden vectors to {out_dir}")
    for m in manifest:
        print(f"  - {m['name']:<28} {m['kind']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
