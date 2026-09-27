#!/usr/bin/env python3
"""
Independent golden-vector generator for the canonical-Huffman container.

This file is a *from-scratch* implementation of the wire format documented in
../../FORMAT.md. It shares no code with the Rust codec: the Huffman tree,
bit packing, container layout and even the decoder are re-implemented here.
CRC-32 comes from Python's zlib (an independent implementation), random data
from a seeded Mersenne Twister.

It self-checks every *valid* vector by decoding it with its own decoder (a
prefix-set walker, a different strategy from the Rust count/base table) before
writing it out. The Rust test-suite then decodes these fixtures with:
  1. the Rust production decoder, and
  2. a second independent decoder written in Rust inside the test crate.

Outputs into <repo>/crates/huff-it/fixtures/:
  *.hfc        raw container bytes
  golden.json  metadata, expected originals and expected failure codes
"""

from __future__ import annotations

import base64
import json
import os
import random
import struct
import zlib
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "crates" / "huff-it" / "fixtures"

MAGIC = b"HUFF"
VERSION = 1
HEADER_LEN = 32
FOOTER_LEN = 16
DIR_ENTRY_LEN = 16
FRAME_OVERHEAD = 8
PAYLOAD_HEADER_LEN = 12
BODY_OFFSET = PAYLOAD_HEADER_LEN + 256
MAX_CODE_LEN = 32
MAX_BLOCK_SIZE = 1 << 20


# ---------------------------------------------------------------- canonical tree
def code_lengths(freq: dict[int, int]) -> dict[int, int]:
    """Depth per symbol, using the normative (weight, height, id) tie-break."""
    syms = sorted(freq)
    n = len(syms)
    if n == 1:
        return {syms[0]: 0}

    # Dense arrays: leaves 0..n-1 in ascending symbol order, internals after.
    left = [-1] * (2 * n - 1)
    right = [-1] * (2 * n - 1)
    height = [0] * (2 * n - 1)
    weight = [0] * (2 * n - 1)
    for i, s in enumerate(syms):
        weight[i] = freq[s]

    heap: list[tuple[int, int, int]] = []
    for i in range(n):
        heappush(heap, (weight[i], 0, i))  # (weight, height, id)

    next_id = n
    while len(heap) > 1:
        w1, h1, id1 = heappop(heap)
        w2, h2, id2 = heappop(heap)
        nid = next_id
        next_id += 1
        weight[nid] = w1 + w2
        height[nid] = max(h1, h2) + 1
        left[nid], right[nid] = id1, id2
        heappush(heap, (weight[nid], height[nid], nid))
    root = next_id - 1

    depths: dict[int, int] = {}
    stack = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if left[node] == -1:
            depths[syms[node]] = depth
        else:
            if depth >= MAX_CODE_LEN:
                raise ValueError("code length exceeds 32")
            stack.append((right[node], depth + 1))
            stack.append((left[node], depth + 1))
    return depths


def canonical_codes(depths: dict[int, int]) -> dict[int, tuple[int, int]]:
    """Assign canonical codes: order by (length, symbol)."""
    out: dict[int, tuple[int, int]] = {}
    code = 0
    prev_len = 0
    for sym in sorted(depths, key=lambda s: (depths[s], s)):
        length = depths[sym]
        if prev_len:
            code = (code + 1) << (length - prev_len)
        out[sym] = (code, length)
        prev_len = length
    return out


# ---------------------------------------------------------------- bit packing
class BitPacker:
    def __init__(self) -> None:
        self.bytes = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> i) & 1)
            self.nbits += 1
            if self.nbits == 8:
                self.bytes.append(self.acc)
                self.acc = 0
                self.nbits = 0

    def finish(self) -> tuple[bytes, int]:
        total = len(self.bytes) * 8 + self.nbits
        if self.nbits:
            self.bytes.append(self.acc << (8 - self.nbits))  # zero padding
        return bytes(self.bytes), total


# ---------------------------------------------------------------- block / container
def encode_block(data: bytes) -> bytes:
    original_len = len(data)
    lengths = [0] * 256
    sole_symbol = 0
    symbol_count = 0
    body = b""
    bits_total = 0

    if data:
        freq: dict[int, int] = {}
        for b in data:
            freq[b] = freq.get(b, 0) + 1
        depths = code_lengths(freq)
        symbol_count = len(depths)
        if symbol_count == 1:
            sole_symbol = next(iter(depths))
        else:
            codes = canonical_codes(depths)
            for s in depths:
                lengths[s] = depths[s]
            packer = BitPacker()
            for b in data:
                code, length = codes[b]
                packer.write(code, length)
            body, bits_total = packer.finish()

    payload = struct.pack(
        "<IIH B B",
        original_len,
        bits_total,
        symbol_count,
        sole_symbol,
        0,  # reserved
    )
    payload += bytes(lengths)
    payload += body
    assert len(payload) == BODY_OFFSET + len(body)
    return payload


@dataclass
class Container:
    block_size: int
    original_total: int
    frames: list[bytes]   # full frame bytes
    payloads: list[bytes]
    originals: list[bytes]

    def serialize(self, version: int = VERSION) -> bytes:
        block_count = len(self.frames)
        dir_offset = HEADER_LEN + sum(len(f) for f in self.frames)
        dir_len = block_count * DIR_ENTRY_LEN

        header = bytearray()
        header += MAGIC
        header += bytes([version, 0])
        header += struct.pack("<I", self.block_size)
        header += struct.pack("<I", self.original_total)
        header += struct.pack("<I", block_count)
        header += struct.pack("<I", dir_offset)
        header += struct.pack("<I", dir_len)
        header += struct.pack("<H", 0)
        header += struct.pack("<I", zlib.crc32(bytes(header[:28])) & 0xFFFFFFFF)
        assert len(header) == HEADER_LEN

        directory = bytearray()
        cursor = HEADER_LEN
        for payload, original in zip(self.payloads, self.originals):
            original_len = struct.unpack("<I", payload[0:4])[0]
            directory += struct.pack(
                "<IIII",
                original_len,
                len(payload),
                cursor,
                zlib.crc32(original) & 0xFFFFFFFF,
            )
            cursor += FRAME_OVERHEAD + len(payload)
        dir_crc = zlib.crc32(bytes(directory)) & 0xFFFFFFFF

        out = bytearray(header)
        for frame in self.frames:
            out += frame
        out += directory
        out += struct.pack("<IIII", dir_crc, dir_offset, dir_len, block_count)
        return bytes(out)


def make_container(data: bytes, block_size: int = 65536) -> Container:
    assert 0 < block_size <= MAX_BLOCK_SIZE
    chunks = [data[i : i + block_size] for i in range(0, len(data), block_size)]
    if not chunks:
        chunks = [b""]
    frames, payloads = [], []
    for chunk in chunks:
        payload = encode_block(chunk)
        frame = struct.pack("<II", len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload
        frames.append(frame)
        payloads.append(payload)
    return Container(block_size, len(data), frames, payloads, list(chunks))


# ---------------------------------------------------------------- independent decoder
def independent_decode_container(blob: bytes) -> bytes:
    """Prefix-set walker — deliberately different from the Rust table decoder."""
    if len(blob) < HEADER_LEN:
        raise ValueError("HEADER_TRUNCATED")
    if blob[0:4] != MAGIC:
        raise ValueError("BAD_MAGIC")
    if blob[4] != VERSION:
        raise ValueError("UNKNOWN_VERSION")
    if blob[5] != 0:
        raise ValueError("UNKNOWN_FLAGS")
    if zlib.crc32(blob[0:28]) & 0xFFFFFFFF != struct.unpack("<I", blob[28:32])[0]:
        raise ValueError("HEADER_CRC_MISMATCH")

    block_size, total, block_count, dir_offset, dir_len = struct.unpack(
        "<IIIII", blob[6:26]
    )
    footer = dir_offset + dir_len
    assert len(blob) == footer + FOOTER_LEN, "TRAILING_DATA"
    d_crc, f_off, f_len, f_bc = struct.unpack("<IIII", blob[footer : footer + 16])
    assert (f_off, f_len, f_bc) == (dir_offset, dir_len, block_count), "DIR_BOUNDS"
    assert zlib.crc32(blob[dir_offset:footer]) & 0xFFFFFFFF == d_crc, "DIR_CRC"

    out = bytearray()
    cursor = HEADER_LEN
    for i in range(block_count):
        e = dir_offset + i * DIR_ENTRY_LEN
        orig_len, payload_len, frame_off, orig_crc = struct.unpack(
            "<IIII", blob[e : e + 16]
        )
        assert frame_off == cursor, "DIR_NOT_CONTIGUOUS"
        start = frame_off + FRAME_OVERHEAD
        payload = blob[start : start + payload_len]
        stored_len, stored_crc = struct.unpack("<II", blob[frame_off : frame_off + 8])
        assert stored_len == payload_len, "DIR_BOUNDS"
        assert zlib.crc32(payload) & 0xFFFFFFFF == stored_crc, "BLOCK_CRC"

        original_len, bits_total, symbol_count = struct.unpack(
            "<IIH", payload[0:10]
        )
        sole_symbol = payload[10]
        lengths = list(payload[PAYLOAD_HEADER_LEN:BODY_OFFSET])
        body = payload[BODY_OFFSET:]
        assert bits_total <= len(body) * 8, "BITSTREAM_TRUNCATED"

        if original_len == 0:
            assert symbol_count == 0 and bits_total == 0 and body == b""
            decoded = b""
        elif symbol_count == 1:
            assert all(l == 0 for l in lengths), "TABLE_SHAPE"
            assert bits_total == 0, "PADDING"
            decoded = bytes([sole_symbol]) * original_len
        else:
            # Exact codewords and the set of all their strict prefixes.
            depths = {s: lengths[s] for s in range(256) if lengths[s] > 0}
            assert len(depths) == symbol_count, "BAD_SYMBOL_COUNT"
            codes = canonical_codes(depths)
            exact = {(code, length): s for s, (code, length) in codes.items()}
            prefixes = set()
            for code, length in codes.values():
                for k in range(1, length):
                    prefixes.add((code >> (length - k), k))

            def bits_of():
                for bit_index in range(bits_total):
                    yield (body[bit_index >> 3] >> (7 - (bit_index & 7))) & 1

            decoded = bytearray()
            code = 0
            length = 0
            for bit in bits_of():
                code = (code << 1) | bit
                length += 1
                if (code, length) in exact:
                    decoded.append(exact[(code, length)])
                    code = 0
                    length = 0
                elif (code, length) not in prefixes:
                    raise ValueError("INVALID_CODEWORD")
            assert length == 0, "TRUNCATED_CODEWORD (codeword unfinished)"
            assert len(decoded) == original_len, "OUTPUT_LENGTH_MISMATCH"
            # Padding bits must be zero.
            for p in range(bits_total, len(body) * 8):
                assert ((body[p >> 3] >> (7 - (p & 7))) & 1) == 0, "INVALID_PADDING"
            decoded = bytes(decoded)

        assert len(decoded) == orig_len, "OUTPUT_LENGTH_MISMATCH"
        assert zlib.crc32(decoded) & 0xFFFFFFFF == orig_crc, "ORIGINAL_CRC"
        out += decoded
        cursor = frame_off + FRAME_OVERHEAD + payload_len

    assert cursor == dir_offset, "DIR_NOT_CONTIGUOUS"
    assert len(out) == total, "TOTAL_LENGTH_MISMATCH"
    return bytes(out)


# ---------------------------------------------------------------- mutation helpers
def recrc_container(
    payloads: list[bytes], originals: list[bytes], block_size: int, version: int = VERSION
) -> bytes:
    frames = [
        struct.pack("<II", len(p), zlib.crc32(p) & 0xFFFFFFFF) + p for p in payloads
    ]
    return Container(block_size, sum(len(o) for o in originals), frames, payloads,
                     list(originals)).serialize(version=version)


def splice_payload(blob: bytes, block_index: int = 0) -> tuple[list[bytes], list[bytes], int, int]:
    """Pull payloads/originals back out of a valid blob (single block expected)."""
    block_size, total, block_count, dir_offset, dir_len = struct.unpack(
        "<IIIII", blob[6:26]
    )
    payloads, originals, cursor = [], [], HEADER_LEN
    for i in range(block_count):
        plen = struct.unpack("<I", blob[cursor : cursor + 4])[0]
        payloads.append(bytes(blob[cursor + 8 : cursor + 8 + plen]))
        e = dir_offset + i * DIR_ENTRY_LEN
        orig_len = struct.unpack("<I", blob[e : e + 4])[0]
        originals.append(b"\x00" * orig_len)  # placeholder; negative cases never decode
        cursor += FRAME_OVERHEAD + plen
    return payloads, originals, block_size, total


# ---------------------------------------------------------------- fixture corpus
def build_fixtures() -> dict:
    cases: list[dict] = []

    def add_valid(name: str, data: bytes, block_size: int = 65536,
                  description: str = "") -> None:
        cont = make_container(data, block_size)
        blob = cont.serialize()
        # Independent self-check before writing anything.
        check = independent_decode_container(blob)
        assert check == data, f"python self-check failed for {name}"
        (OUT_DIR / f"{name}.hfc").write_bytes(blob)
        cases.append({
            "name": name,
            "kind": "roundtrip",
            "description": description,
            "file": f"{name}.hfc",
            "original_len": len(data),
            "original_sha256": __import__("hashlib").sha256(data).hexdigest(),
            "original_b64": base64.b64encode(data).decode(),
            "container_len": len(blob),
        })

    # --- valid / roundtrip corpus -------------------------------------------------
    add_valid("empty", b"", description="empty input: empty alphabet, empty block")
    add_valid("single", b"Q", description="one byte: one-symbol length-0 convention")
    add_valid("one_symbol_run", b"A" * 5000,
              description="extreme skew to a single symbol (5000 copies, 0 body bits)")
    add_valid("all_bytes", bytes(range(256)),
              description="each byte value exactly once (256-symbol alphabet)")
    skew = b"\x00" * 20000 + bytes(range(256)) + b"\x00" * 20000
    add_valid("extreme_skew", skew, block_size=4096,
              description="40k zero bytes plus one of every value; multi-block")
    rng = random.Random(20260927)
    add_valid("random_seeded", rng.randbytes(5000), block_size=4096,
              description="deterministic Mersenne-Twister pseudo-random bytes, 2 blocks")
    add_valid("text", b"the quick brown fox jumps over the lazy dog\n" * 13,
              description="repetitive natural-language-ish text")

    # --- negative corpus ----------------------------------------------------------
    def add_negative(name: str, blob: bytes, expected_error: str, description: str) -> None:
        (OUT_DIR / f"{name}.hfc").write_bytes(blob)
        cases.append({
            "name": name,
            "kind": "negative",
            "description": description,
            "file": f"{name}.hfc",
            "expected_error": expected_error,
            "container_len": len(blob),
        })

    # 1. Over-complete (oversubscribed) code tree: three length-1 codes, Kraft 3/2.
    base = make_container(b"\x00\x01\x02hello")
    payload = bytearray(base.payloads[0])
    lengths = [0] * 256
    lengths[0] = lengths[1] = lengths[2] = 1
    payload[0:4] = struct.pack("<I", 3)      # original_len
    payload[4:8] = struct.pack("<I", 0)      # bits_total
    payload[8:10] = struct.pack("<H", 3)     # symbol_count
    payload[10] = 0                          # sole_symbol
    payload[PAYLOAD_HEADER_LEN:BODY_OFFSET] = bytes(lengths)
    # Truncate body region to zero by rebuilding: BODY_OFFSET is fixed; keep body
    # but declare zero bits. The decoder rejects at Kraft before reading bits.
    payload[BODY_OFFSET:] = b""
    blob = recrc_payload_solo(bytes(payload), orig_len=3, block_size=base.block_size)
    add_negative("oversubscribed_tree", blob, "TABLE_KRAFT_OVERFLOW",
                 "three codewords of length 1 violate the Kraft inequality")

    # 2. Truncated codeword: valid table, bits_total shortened into a codeword.
    blob = (OUT_DIR / "all_bytes.hfc").read_bytes()
    payloads, originals, block_size, _ = splice_payload(blob)
    p = bytearray(payloads[0])
    bits_total = struct.unpack("<I", p[4:8])[0]
    assert bits_total > 8, bits_total
    p[4:8] = struct.pack("<I", 3)  # only 3 valid bits; shortest codes here are longer
    payloads[0] = bytes(p)
    blob = recrc_payload_solo_from_parts(payloads, blob)
    add_negative("truncated_codeword", blob, "TRUNCATED_CODEWORD",
                 "bitstream ends in the middle of a codeword (bits_total=3)")

    # 3. Wrong padding bits: flip the first bit after the valid stream.
    source = b"padding bits must stay zero!!"
    base2 = make_container(source)
    p2 = bytearray(base2.payloads[0])
    bits_total = struct.unpack("<I", p2[4:8])[0]
    if bits_total % 8 == 0:  # guarantee padding exists by adjusting the input
        source = source + b"?"
        base2 = make_container(source)
        p2 = bytearray(base2.payloads[0])
        bits_total = struct.unpack("<I", p2[4:8])[0]
    assert bits_total % 8 != 0
    byte_i = BODY_OFFSET + (bits_total >> 3)
    bit_in_byte = bits_total & 7
    p2[byte_i] |= 0x80 >> bit_in_byte
    blob = recrc_payload_solo(bytes(p2), orig_len=len(source),
                              block_size=base2.block_size, original=source)
    add_negative("bad_padding_bits", blob, "INVALID_PADDING",
                 "first padding bit after the final codeword is 1")

    # 4. Unknown container version (header CRC recomputed for the bumped byte).
    blob = make_container(b"version probe").serialize()
    blob_v2 = bytearray(blob)
    blob_v2[4] = 99
    blob_v2[28:32] = struct.pack("<I", zlib.crc32(bytes(blob_v2[:28])) & 0xFFFFFFFF)
    add_negative("unknown_version", bytes(blob_v2), "UNKNOWN_VERSION",
                 "version byte 99 must be refused, not guessed")

    return {
        "format_version": VERSION,
        "generator": "tools/gen_golden.py (independent Python implementation)",
        "cases": cases,
    }


def recrc_payload_solo(payload: bytes, orig_len: int, block_size: int,
                       original: bytes = b"") -> bytes:
    """Rebuild a single-block container around one (possibly malformed) payload."""
    frame = struct.pack("<II", len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload
    dir_offset = HEADER_LEN + len(frame)
    header = bytearray()
    header += MAGIC + bytes([VERSION, 0])
    header += struct.pack("<I", block_size)
    header += struct.pack("<I", orig_len)
    header += struct.pack("<I", 1)
    header += struct.pack("<I", dir_offset)
    header += struct.pack("<I", DIR_ENTRY_LEN)
    header += struct.pack("<H", 0)
    header += struct.pack("<I", zlib.crc32(bytes(header[:28])) & 0xFFFFFFFF)
    directory = struct.pack(
        "<IIII", orig_len, len(payload), HEADER_LEN,
        zlib.crc32(original) & 0xFFFFFFFF,
    )
    out = bytearray(header) + frame + directory
    out += struct.pack(
        "<IIII",
        zlib.crc32(directory) & 0xFFFFFFFF,
        dir_offset, DIR_ENTRY_LEN, 1,
    )
    return bytes(out)


def recrc_payload_solo_from_parts(payloads: list[bytes], reference_blob: bytes) -> bytes:
    """Re-serialize using the reference blob's metadata (truncated-codeword case)."""
    block_size, total, block_count, _, _ = struct.unpack(
        "<IIIII", reference_blob[6:26]
    )
    assert block_count == 1
    orig_len = struct.unpack("<I", payloads[0][0:4])[0]
    return recrc_payload_solo(payloads[0], orig_len=orig_len, block_size=block_size)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*"):
        old.unlink()
    manifest = build_fixtures()
    (OUT_DIR / "golden.json").write_text(json.dumps(manifest, indent=2) + "\n")
    valid = sum(1 for c in manifest["cases"] if c["kind"] == "roundtrip")
    neg = len(manifest["cases"]) - valid
    print(f"wrote {valid} roundtrip + {neg} negative golden vectors to {OUT_DIR}")
    for case in manifest["cases"]:
        print(f"  {case['name']:<22} {case['kind']:<9} {case.get('expected_error', 'ok')}")


if __name__ == "__main__":
    main()
