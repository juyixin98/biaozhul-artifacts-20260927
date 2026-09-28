#!/usr/bin/env python3
"""
Independent LZ77-block reference implementation and oracle.

This file is deliberately written from the wire-format specification alone and
shares NO code with the Rust implementation under test:

  * encoder: naive brute-force longest-match scan (the Rust side uses hash chains);
  * integrity: zlib.crc32 and hashlib.sha256 (the Rust side has hand-rolled CRC);
  * decoder: independent token walker, byte-at-a-time overlap copy.

It serves three roles in the test suite:
  1. reference DECODER (oracle) for frames the Rust encoder produced;
  2. independent ENCODER whose frames the Rust decoder must accept;
  3. fixture/malformed-stream generator (see `gen-fixtures`).

Wire format (little endian):
  magic "LZ71" | ver 0x01 | flags | digest[32] | data_len u64 | payload_len u64
  | crc32 u32 | payload...
  payload tokens: 0x00 <byte>            literal
                  0x01 <dist u16> <len>  copy len+3 from dist bytes back
                  0xff                   end

CLI (all machine-readable):
  encode    --in RAW --out FRAME --mode independent|dependent [--dict FILE]
  decode    --in FRAME [--dict FILE] --out RAW --report JSON
  gen-fixtures --out DIR
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
import sys
import zlib

WINDOW_BYTES = 4096
MIN_MATCH = 3
MAX_MATCH = 258
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_EXPANSION_RATIO = 100

CTRL_LITERAL = 0x00
CTRL_MATCH = 0x01
CTRL_END = 0xFF

MAGIC = b"LZ71"
VERSION = 0x01
FLAG_DEPENDENT = 0x01
HEADER_LEN = 58

# Category strings mirror src/error.rs exactly.
CAT_INPUT = "input_error"
CAT_STATE = "state_conflict"
CAT_EXHAUSTED = "resource_exhausted"


class RefError(Exception):
    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category = category
        self.detail = detail


# --------------------------------------------------------------------------- kernel

def tail_window(data: bytes) -> bytes:
    return data[-WINDOW_BYTES:] if len(data) > WINDOW_BYTES else data


def compress(data: bytes, dictionary: bytes) -> bytes:
    """Brute-force greedy LZ77. O(n*W) by design: clarity over speed."""
    if len(dictionary) > WINDOW_BYTES:
        raise RefError("compute_failure", "dictionary longer than window")
    work = dictionary + data
    start = len(dictionary)
    end = len(work)
    out = bytearray()
    pos = start
    while pos < end:
        best_len = 0
        best_dist = 0
        scan_lo = max(0, pos - WINDOW_BYTES)
        limit = min(end - pos, MAX_MATCH)
        for cand in range(scan_lo, pos):
            dist = pos - cand
            # cheap reject
            if best_len and work[cand + best_len] != work[pos + best_len]:
                continue
            ln = 0
            while ln < limit and work[cand + ln] == work[pos + ln]:
                ln += 1
            if ln > best_len:
                best_len, best_dist = ln, dist
                if ln == limit:
                    break
        if best_len >= MIN_MATCH:
            out.append(CTRL_MATCH)
            out += struct.pack("<H", best_dist)
            out.append(best_len - MIN_MATCH)
            pos += best_len
        else:
            out.append(CTRL_LITERAL)
            out.append(work[pos])
            pos += 1
    out.append(CTRL_END)
    return bytes(out)


def decode(payload: bytes, dictionary: bytes, declared_len: int) -> tuple[bytes, dict]:
    """Byte-at-a-time decoder. Returns (block_bytes, intermediate_stats)."""
    if len(dictionary) > WINDOW_BYTES:
        raise RefError("compute_failure", "dictionary longer than window")
    work = bytearray(dictionary)
    block_start = len(work)
    declared = declared_len
    cur = 0
    stats = {
        "tokens_literal": 0,
        "tokens_match": 0,
        "overlap_copies": 0,
        "boundary_copies": 0,   # distance exactly WINDOW_BYTES
        "max_distance": 0,
        "match_bytes": 0,
        "literal_bytes": 0,
    }
    n = len(payload)
    while True:
        if cur >= n:
            raise RefError(CAT_INPUT, f"truncated stream: no end marker at offset {cur}")
        ctrl = payload[cur]
        cur += 1
        if ctrl == CTRL_LITERAL:
            if cur >= n:
                raise RefError(CAT_INPUT, "literal token missing its byte")
            if len(work) - block_start >= declared:
                raise RefError(CAT_INPUT, "stream produces more bytes than data_len declares")
            work.append(payload[cur])
            cur += 1
            stats["tokens_literal"] += 1
            stats["literal_bytes"] += 1
        elif ctrl == CTRL_MATCH:
            if cur + 3 > n:
                raise RefError(CAT_INPUT, "match token truncated (need dist u16 + len)")
            distance = struct.unpack_from("<H", payload, cur)[0]
            length = payload[cur + 2] + MIN_MATCH
            cur += 3
            if distance == 0:
                raise RefError(CAT_INPUT, "match with distance 0")
            if distance > WINDOW_BYTES:
                raise RefError(
                    CAT_INPUT,
                    f"match distance {distance} exceeds fixed window {WINDOW_BYTES}",
                )
            if distance > len(work):
                raise RefError(
                    CAT_INPUT,
                    f"match distance {distance} points before start of stream "
                    f"(available history {len(work)} bytes)",
                )
            if len(work) - block_start + length > declared:
                raise RefError(
                    CAT_INPUT,
                    f"match of {length} bytes would overrun declared data_len {declared}",
                )
            src = len(work) - distance
            if length > distance:
                stats["overlap_copies"] += 1
            if distance == WINDOW_BYTES:
                stats["boundary_copies"] += 1
            stats["tokens_match"] += 1
            stats["max_distance"] = max(stats["max_distance"], distance)
            stats["match_bytes"] += length
            for k in range(length):  # indexed: overlapping copies see freshly written bytes
                work.append(work[src + k])
        elif ctrl == CTRL_END:
            if cur != n:
                raise RefError(CAT_INPUT, f"{n - cur} trailing octets after end marker")
            produced = len(work) - block_start
            if produced != declared:
                raise RefError(
                    CAT_INPUT,
                    f"stream produced {produced} bytes but data_len declares {declared}",
                )
            return bytes(work[block_start:]), stats
        else:
            raise RefError(CAT_INPUT, f"unknown control byte 0x{ctrl:02x} at offset {cur - 1}")


# --------------------------------------------------------------------------- frames

def dict_digest(dictionary: bytes) -> bytes:
    return hashlib.sha256(tail_window(dictionary)).digest()


def encode_frame(mode: str, data: bytes, dictionary: bytes = b"") -> bytes:
    if mode == "independent":
        if dictionary:
            raise RefError(CAT_STATE, "independent block cannot take a dictionary")
        payload = compress(data, b"")
        digest = b"\x00" * 32
    elif mode == "dependent":
        window = tail_window(dictionary)
        payload = compress(data, window)
        digest = dict_digest(window)
    else:
        raise RefError(CAT_INPUT, f"unknown mode {mode!r}")
    header = (
        MAGIC
        + bytes([VERSION, 0 if mode == "independent" else FLAG_DEPENDENT])
        + digest
        + struct.pack("<Q", len(data))
        + struct.pack("<Q", len(payload))
        + struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)
    )
    return header + payload


def parse_frame(buf: bytes) -> dict:
    """Full envelope validation; mirrors format.rs::decode_frame."""
    if len(buf) < HEADER_LEN:
        raise RefError(CAT_INPUT, f"frame too short: {len(buf)} bytes < header {HEADER_LEN}")
    if buf[0:4] != MAGIC:
        raise RefError(CAT_INPUT, f"bad magic: {buf[0:4]!r}")
    if buf[4] != VERSION:
        raise RefError(CAT_INPUT, f"unsupported version 0x{buf[4]:02x}")
    flags = buf[5]
    if flags & ~FLAG_DEPENDENT:
        raise RefError(CAT_INPUT, f"reserved flag bits set: 0x{flags:02x}")
    mode = "dependent" if flags & FLAG_DEPENDENT else "independent"
    digest = buf[6:38]
    data_len = struct.unpack_from("<Q", buf, 38)[0]
    payload_len = struct.unpack_from("<Q", buf, 46)[0]
    crc = struct.unpack_from("<I", buf, 54)[0]
    if mode == "independent" and digest != b"\x00" * 32:
        raise RefError(CAT_INPUT, "independent block carries a nonzero dictionary digest")
    if payload_len == 0:
        raise RefError(CAT_INPUT, "payload length is zero")
    if payload_len != len(buf) - HEADER_LEN:
        raise RefError(
            CAT_INPUT,
            f"payload length mismatch: header declares {payload_len}, "
            f"frame holds {len(buf) - HEADER_LEN}",
        )
    payload = buf[HEADER_LEN:]
    if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
        raise RefError(
            CAT_INPUT,
            f"payload CRC mismatch: frame 0x{crc:08x}, computed "
            f"0x{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
        )
    if data_len > MAX_OUTPUT_BYTES:
        raise RefError(
            CAT_EXHAUSTED,
            f"declared output {data_len} bytes exceeds per-block cap {MAX_OUTPUT_BYTES}",
        )
    if data_len > payload_len * MAX_EXPANSION_RATIO:
        raise RefError(
            CAT_EXHAUSTED,
            f"declared expansion {payload_len}->{data_len} bytes exceeds ratio "
            f"{MAX_EXPANSION_RATIO}x",
        )
    return {
        "mode": mode,
        "digest": digest,
        "data_len": data_len,
        "payload": payload,
        "crc32": crc,
    }


def decode_frame(buf: bytes, dictionary: bytes = b"") -> dict:
    env = parse_frame(buf)
    window = tail_window(dictionary)
    if env["mode"] == "independent":
        if window:
            raise RefError(
                CAT_STATE,
                f"independent block cannot be decoded against a {len(window)} byte dictionary",
            )
    else:
        want = dict_digest(window)
        if want != env["digest"]:
            raise RefError(
                CAT_STATE,
                "dictionary digest mismatch: block expects "
                f"{env['digest'].hex()}, receiver has {want.hex()}",
            )
    out, stats = decode(env["payload"], window, env["data_len"])
    return {
        "valid": True,
        "mode": env["mode"],
        "data_len": env["data_len"],
        "payload_len": len(env["payload"]),
        "crc32": f"{env['crc32']:08x}",
        "dict_digest": env["digest"].hex(),
        "sha256": hashlib.sha256(out).hexdigest(),
        "stats": stats,
        "bytes": out,
    }


# ---------------------------------------------------------------------- raw crafting

def craft_frame(flags: int, digest: bytes, data_len: int, payload: bytes,
                crc: int | None = None, bad_payload_len: int | None = None) -> bytes:
    """Build a frame with attacker-controlled fields (bypasses validation)."""
    if crc is None:
        crc = zlib.crc32(payload) & 0xFFFFFFFF
    plen = bad_payload_len if bad_payload_len is not None else len(payload)
    return (
        MAGIC
        + bytes([VERSION, flags])
        + digest
        + struct.pack("<Q", data_len)
        + struct.pack("<Q", plen)
        + struct.pack("<I", crc)
        + payload
    )


# ------------------------------------------------------------------- deterministic data

def synthetic_source(total: int = 20_000) -> bytes:
    """Deterministic synthetic corpus, NO external/random sources.

    Layout (explicit markers make cross-window matches auditable):
      [0:64)          fixed poem-like phrase
      repeated phrase blocks with counters
      pseudo-random section from a fixed-seed LCG
      a tail engineered to reference the very first bytes at distance 4096
    """
    parts = []
    phrase = b"the quick brown fox jumps over the lazy dog. "
    parts.append(phrase)
    # repeated/counter section
    i = 0
    while sum(len(p) for p in parts) < 6_000:
        parts.append(phrase + b"block=" + str(i).encode() + b"; ")
        i += 1
    # LCG pseudo-random section (glibc-style constants, fixed seed)
    state = 0x1234_5678
    rand = bytearray()
    while len(rand) < 6_000:
        state = (1103515245 * state + 12345) & 0x7FFF_FFFF
        rand.append((state >> 8) & 0xFF)
    parts.append(bytes(rand))
    data = b"".join(parts)
    # Engineered cross-window tail: pad so that a fresh 3-byte signature lands with
    # its only earlier occurrence exactly WINDOW_BYTES behind.
    sig = b"\x5a\x51\xa3"  # unlikely inside the sections above
    head = sig + data[:3]
    data = head + data[3:]
    # Pad up to the boundary: we want `sig` repeated at file offset WINDOW_BYTES.
    if len(data) < WINDOW_BYTES:
        data += bytes([0x77]) * (WINDOW_BYTES - len(data))
    # insert signature copy at WINDOW_BYTES and a few follow bytes copied from pos 3
    tail = bytearray(data[:WINDOW_BYTES])
    follow = data[3:3 + 8]
    tail += sig + follow + data[WINDOW_BYTES:]
    data = bytes(tail)
    if len(data) < total:
        data += bytes([0x77]) * (total - len(data))
    return data[:total]


# --------------------------------------------------------------------- fixture generation

def sha_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write_fixtures(out_dir: str) -> dict:
    os.makedirs(os.path.join(out_dir, "good"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "good", "three_independent"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "good", "chain"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "good", "chain_alt"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "malformed"), exist_ok=True)

    manifest = {"constants": {
        "window_bytes": WINDOW_BYTES, "min_match": MIN_MATCH, "max_match": MAX_MATCH,
        "max_output_bytes": MAX_OUTPUT_BYTES, "max_expansion_ratio": MAX_EXPANSION_RATIO,
    }, "cases": []}

    def add_case(kind, name, path, expect, extra=None):
        entry = {"kind": kind, "name": name, "file": os.path.relpath(path, out_dir),
                 "expect": expect}
        if extra:
            entry.update(extra)
        manifest["cases"].append(entry)

    source = synthetic_source()
    with open(os.path.join(out_dir, "source.bin"), "wb") as f:
        f.write(source)

    # --- good case 1: whole source as one independent block
    frame = encode_frame("independent", source)
    p = os.path.join(out_dir, "good", "one_independent.frame")
    with open(p, "wb") as f:
        f.write(frame)
    rep = decode_frame(frame)
    assert rep["bytes"] == source
    add_case("good", "one_independent", p, {"valid": True},
             {"output_sha256": sha_hex(source), "frame_sha256": sha_hex(frame),
              "frame_len": len(frame), "stats": rep["stats"]})

    # --- good case 2: three independent blocks over equal thirds
    bounds = [0, len(source) // 3, 2 * len(source) // 3, len(source)]
    for idx in range(3):
        chunk = source[bounds[idx]:bounds[idx + 1]]
        fr = encode_frame("independent", chunk)
        p = os.path.join(out_dir, "good", "three_independent", f"{idx}.frame")
        with open(p, "wb") as f:
            f.write(fr)
        r = decode_frame(fr)
        assert r["bytes"] == chunk
        add_case("good", f"three_independent_{idx}", p, {"valid": True},
                 {"slice": [bounds[idx], bounds[idx + 1]],
                  "output_sha256": sha_hex(chunk), "stats": r["stats"]})

    # --- good case 3: independent root + 2 dependent blocks (shared dictionary)
    dep_bounds = [0, 7_000, 13_500, len(source)]
    combined = b""
    for idx in range(3):
        chunk = source[dep_bounds[idx]:dep_bounds[idx + 1]]
        mode = "independent" if idx == 0 else "dependent"
        fr = encode_frame(mode, chunk, combined)
        p = os.path.join(out_dir, "good", "chain", f"{idx}.frame")
        with open(p, "wb") as f:
            f.write(fr)
        r = decode_frame(fr, combined)
        assert r["bytes"] == chunk
        add_case("good", f"chain_{idx}", p, {"valid": True},
                 {"mode": mode, "slice": [dep_bounds[idx], dep_bounds[idx + 1]],
                  "dict_len": len(tail_window(combined)),
                  "dict_digest": dict_digest(combined).hex(),
                  "frame_digest": r["dict_digest"],
                  "output_sha256": sha_hex(chunk), "stats": r["stats"]})
        combined += chunk
    assert combined == source
    manifest["chain_output_sha256"] = sha_hex(source)

    # --- good case 4: different split boundaries, root + 4 dependents
    alt_bounds = [0, 3_333, 8_000, 12_123, 16_777, len(source)]
    combined = b""
    for idx in range(5):
        chunk = source[alt_bounds[idx]:alt_bounds[idx + 1]]
        mode = "independent" if idx == 0 else "dependent"
        fr = encode_frame(mode, chunk, combined)
        p = os.path.join(out_dir, "good", "chain_alt", f"{idx}.frame")
        with open(p, "wb") as f:
            f.write(fr)
        r = decode_frame(fr, combined)
        assert r["bytes"] == chunk
        add_case("good", f"chain_alt_{idx}", p, {"valid": True},
                 {"mode": mode, "slice": [alt_bounds[idx], alt_bounds[idx + 1]],
                  "dict_digest": dict_digest(combined).hex()})
        combined += chunk

    # --- good case 5: self-overlap long match (dist 1, len 258 -> 259 'a's)
    payload = bytes([CTRL_LITERAL, ord("a"), CTRL_MATCH, 1, 0, 258 - MIN_MATCH, CTRL_END])
    fr = craft_frame(0, b"\x00" * 32, 259, payload)
    p = os.path.join(out_dir, "good", "self_overlap_rle.frame")
    with open(p, "wb") as f:
        f.write(fr)
    expected = b"a" * 259
    r = decode_frame(fr)
    assert r["bytes"] == expected
    add_case("good", "self_overlap_rle", p, {"valid": True},
             {"output_sha256": sha_hex(expected), "stats": r["stats"],
              "rationale": "match length 258 > distance 1: byte-at-a-time copy repeats 'a'"})

    # --- good case 6: match at exactly distance WINDOW_BYTES
    head = b"XYZ" + bytes([0x20]) * (WINDOW_BYTES - 3)
    body_cross = head + b"XYZtail"
    fr = encode_frame("independent", body_cross)
    p = os.path.join(out_dir, "good", "cross_window_boundary.frame")
    with open(p, "wb") as f:
        f.write(fr)
    r = decode_frame(fr)
    assert r["bytes"] == body_cross
    add_case("good", "cross_window_boundary", p, {"valid": True},
             {"output_sha256": sha_hex(body_cross), "stats": r["stats"],
              "rationale": f"second XYZ is matched at distance exactly {WINDOW_BYTES}"})

    # --- malformed: truncated header
    p = os.path.join(out_dir, "malformed", "truncated_header.frame")
    with open(p, "wb") as f:
        f.write(MAGIC + b"\x01\x00")
    add_case("bad", "truncated_header", p,
             {"valid": False, "category": CAT_INPUT,
              "rationale": "fewer than 58 header bytes"})

    # --- malformed: bad magic
    p = os.path.join(out_dir, "malformed", "bad_magic.frame")
    with open(p, "wb") as f:
        f.write(b"XXXX" + craft_frame(0, b"\x00" * 32, 0, bytes([CTRL_END]))[4:])
    add_case("bad", "bad_magic", p, {"valid": False, "category": CAT_INPUT})

    # --- malformed: bad crc
    good = craft_frame(0, b"\x00" * 32, 1, bytes([CTRL_LITERAL, 0x41, CTRL_END]))
    bad = bytearray(good)
    bad[-1] ^= 0xFF  # flip end marker -> CRC bytes stay but payload content changes
    # Recompute header CRC to mismatch the payload: overwrite CRC field instead.
    bad = bytearray(good)
    struct.pack_into("<I", bad, 54, struct.unpack_from("<I", good, 54)[0] ^ 0xDEAD_BEEF)
    p = os.path.join(out_dir, "malformed", "bad_crc.frame")
    with open(p, "wb") as f:
        f.write(bytes(bad))
    add_case("bad", "bad_crc", p, {"valid": False, "category": CAT_INPUT,
                                   "rationale": "CRC field corrupted"})

    # --- malformed: distance pointing before stream start
    payload = bytes([CTRL_MATCH, 10, 0, 0, CTRL_END])
    fr = craft_frame(0, b"\x00" * 32, 3, payload)
    p = os.path.join(out_dir, "malformed", "bad_distance_before_start.frame")
    with open(p, "wb") as f:
        f.write(fr)
    add_case("bad", "bad_distance_before_start", p, {"valid": False, "category": CAT_INPUT,
            "intermediate": {"distance": 10, "history": 0},
            "rationale": "first token copies from 10 bytes back but history is empty"})

    # --- malformed: distance 4097 beyond fixed window
    payload = bytes([CTRL_MATCH, 0x01, 0x10, 0, CTRL_END])
    fr = craft_frame(0, b"\x00" * 32, 3, payload)
    p = os.path.join(out_dir, "malformed", "distance_beyond_window.frame")
    with open(p, "wb") as f:
        f.write(fr)
    add_case("bad", "distance_beyond_window", p, {"valid": False, "category": CAT_INPUT,
            "intermediate": {"distance": 4097, "window": WINDOW_BYTES},
            "rationale": "u16 distance field can encode 4097 but fixed window is 4096"})

    # --- state conflict: dependent block decoded with no predecessor
    dep = encode_frame("dependent", b"child data 123", b"root history bytes")
    p = os.path.join(out_dir, "malformed", "dependent_missing_predecessor.frame")
    with open(p, "wb") as f:
        f.write(dep)
    add_case("bad", "dependent_missing_predecessor", p,
             {"valid": False, "category": CAT_STATE},
             {"frame_digest": parse_frame(dep)["digest"].hex(),
              "rationale": "nonzero bound digest presented against empty dictionary"})

    # --- state conflict: right predecessor digest, wrong dictionary bytes
    p = os.path.join(out_dir, "malformed", "dependent_wrong_dictionary.frame")
    with open(p, "wb") as f:
        f.write(dep)
    add_case("bad", "dependent_wrong_dictionary", p,
             {"valid": False, "category": CAT_STATE,
              "decode_with_dictionary_b64": base64.b64encode(b"root history BYTES").decode()},
             {"rationale": "same length dictionary, different bytes -> digest mismatch"})

    # --- resource exhaustion: declared 1 GiB, tiny valid payload
    fr = craft_frame(0, b"\x00" * 32, 1 << 30, bytes([CTRL_END]))
    p = os.path.join(out_dir, "malformed", "bomb_declared_size.frame")
    with open(p, "wb") as f:
        f.write(fr)
    add_case("bad", "bomb_declared_size", p,
             {"valid": False, "category": CAT_EXHAUSTED},
             {"declared_data_len": 1 << 30, "payload_len": 1,
              "rationale": "declared output exceeds 8 MiB absolute cap; no allocation allowed"})

    # --- resource exhaustion: expansion ratio attack (RLE spam, 10 MiB declared)
    spam = bytearray([CTRL_LITERAL, ord("z")])
    spam += bytes([CTRL_MATCH, 1, 0, 258 - MIN_MATCH]) * 40
    spam.append(CTRL_END)
    fr = craft_frame(0x01, b"\x11" * 32, 20_000_000, bytes(spam))
    p = os.path.join(out_dir, "malformed", "bomb_expansion_ratio.frame")
    with open(p, "wb") as f:
        f.write(fr)
    add_case("bad", "bomb_expansion_ratio", p,
             {"valid": False, "category": CAT_EXHAUSTED},
             {"declared_data_len": 20_000_000, "payload_len": len(spam),
              "ratio": 20_000_000 / len(spam),
              "rationale": "declared ratio exceeds 100x; rejected from header fields alone"})

    # --- input error: stream overproduces vs declaration
    payload = bytes([CTRL_LITERAL, 0x41, CTRL_END])
    fr = craft_frame(0, b"\x00" * 32, 0, payload)
    p = os.path.join(out_dir, "malformed", "overproduces.frame")
    with open(p, "wb") as f:
        f.write(fr)
    add_case("bad", "overproduces", p, {"valid": False, "category": CAT_INPUT,
            "rationale": "data_len says 0 bytes but payload emits a literal"})

    # --- input error: trailing bytes after END
    payload = bytes([CTRL_END, 0x00])
    fr = craft_frame(0, b"\x00" * 32, 0, payload)
    p = os.path.join(out_dir, "malformed", "trailing_bytes.frame")
    with open(p, "wb") as f:
        f.write(fr)
    add_case("bad", "trailing_bytes", p, {"valid": False, "category": CAT_INPUT})

    # --- input error: unknown control byte
    payload = bytes([0x07, CTRL_END])
    fr = craft_frame(0, b"\x00" * 32, 0, payload)
    p = os.path.join(out_dir, "malformed", "unknown_control.frame")
    with open(p, "wb") as f:
        f.write(fr)
    add_case("bad", "unknown_control", p, {"valid": False, "category": CAT_INPUT})

    with open(os.path.join(out_dir, "fixtures_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest


# --------------------------------------------------------------------------- CLI

def cmd_encode(args) -> int:
    data = sys.stdin.buffer.read() if args.in_path == "-" else open(args.in_path, "rb").read()
    dictionary = b""
    if args.dict:
        dictionary = open(args.dict, "rb").read()
    frame = encode_frame(args.mode, data, dictionary)
    out = sys.stdout.buffer if args.out_path == "-" else open(args.out_path, "wb")
    out.write(frame)
    return 0


def cmd_decode(args) -> int:
    buf = sys.stdin.buffer.read() if args.in_path == "-" else open(args.in_path, "rb").read()
    dictionary = open(args.dict, "rb").read() if args.dict else b""
    report: dict
    try:
        report = decode_frame(buf, dictionary)
        raw = report.pop("bytes")
        if args.out_path:
            with open(args.out_path, "wb") as f:
                f.write(raw)
        report["out_len"] = len(raw)
    except RefError as e:
        report = {"valid": False, "error_category": e.category, "reason": e.detail}
    report["python_implementation"] = "reference/ref_lz77.py (brute-force, zlib, hashlib)"
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        with open(args.report, "w") as f:
            f.write(payload + "\n")
    else:
        print(payload)
    return 0 if report.get("valid") else 2


def cmd_gen(args) -> int:
    m = write_fixtures(args.out_dir)
    print(json.dumps({"generated": args.out_dir, "cases": len(m["cases"])}))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode")
    e.add_argument("--in", dest="in_path", required=True)
    e.add_argument("--out", dest="out_path", required=True)
    e.add_argument("--mode", choices=["independent", "dependent"], required=True)
    e.add_argument("--dict")
    e.set_defaults(func=cmd_encode)

    d = sub.add_parser("decode")
    d.add_argument("--in", dest="in_path", required=True)
    d.add_argument("--out", dest="out_path")
    d.add_argument("--dict")
    d.add_argument("--report")
    d.set_defaults(func=cmd_decode)

    g = sub.add_parser("gen-fixtures")
    g.add_argument("--out", dest="out_dir", required=True)
    g.set_defaults(func=cmd_gen)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
