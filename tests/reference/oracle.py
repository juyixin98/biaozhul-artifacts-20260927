#!/usr/bin/env python3
"""
Independent reference oracle for the erasure-coding service.

This file shares NO code with the Rust implementation. It independently
re-derives every fixed convention and emits golden vectors that the Rust
integration tests compare against byte-for-byte:

  * GF(2^8) arithmetic  — modulus 0x11B (AES polynomial), log/exp built from
    generator 3 (NOT 2: in the AES field 2 is not a primitive element).
  * Coding matrix       — systematic [I_k | Cauchy], C[p][j] = 1/(x_p XOR y_j),
    y_j = j, x_p = k + p.
  * Shard layout        — zero-pad original to k*shard_len, split contiguously.
  * Shard digest        — SHA-256(b"ec-shard-v1" || u16be(index) ||
                          u64be(len) || bytes).
  * Manifest            — the TLV covered encoding documented in
    crates/ec-format/src/lib.rs, then SHA-256.

Only the Python standard library is used (hashlib, json, struct, argparse).

Usage:
  python3 oracle.py emit --out fixtures.json
  python3 oracle.py recover --manifest manifest.json --shards dir/
"""

import argparse
import base64
import hashlib
import json
import os
import struct
import sys

MODULUS = 0x11B
GENERATOR = 3

# ---------------------------------------------------------------- GF(2^8)

def _build_tables():
    exp = [0] * 256
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = _mul_slow(x, GENERATOR)
    assert x == 1, "3 must generate the group under modulus 0x11B"
    exp[255] = exp[0]
    return exp, log


def _mul_slow(a, b):
    """Textbook shift-and-XOR multiply with explicit reduction."""
    result = 0
    cur = a
    for _ in range(8):
        if b & 1:
            result ^= cur
        b >>= 1
        cur <<= 1
        if cur & 0x100:
            cur ^= MODULUS
        cur &= 0xFF
    return result


EXP, LOG = _build_tables()


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return EXP[(LOG[a] + LOG[b]) % 255]


def gf_inv(a):
    if a == 0:
        raise ZeroDivisionError("inverse of zero")
    return EXP[255 - LOG[a]]


# --------------------------------------------------------------- coding

def cauchy_entry(x, y):
    assert x != y
    return gf_inv(x ^ y)


def parity_row(k, p):
    x = k + p
    return [cauchy_entry(x, j) for j in range(k)]


def apply_row(row, data_shards):
    length = len(data_shards[0])
    out = bytearray(length)
    for j, coef in enumerate(row):
        if coef == 0:
            continue
        shard = data_shards[j]
        for off in range(length):
            out[off] ^= gf_mul(coef, shard[off])
    return bytes(out)


def encode(k, m, original: bytes):
    shard_len = max(1, (len(original) + k - 1) // k)
    padded = original + b"\x00" * (k * shard_len - len(original))
    data = [padded[j * shard_len:(j + 1) * shard_len] for j in range(k)]
    shards = list(data)
    for p in range(m):
        shards.append(apply_row(parity_row(k, p), data))
    return shard_len, shards


def gauss_solve(matrix, rhs_shards):
    """Solve A X = B; rhs_shards are k byte rows. Returns data shards."""
    k = len(matrix)
    aug = [list(matrix[r]) + list(rhs_shards[r]) for r in range(k)]
    width = k + len(rhs_shards[0])
    for col in range(k):
        pivot = next(r for r in range(col, k) if aug[r][col] != 0)
        aug[col], aug[pivot] = aug[pivot], aug[col]
        inv = gf_inv(aug[col][col])
        for c in range(col, width):
            aug[col][c] = gf_mul(aug[col][c], inv)
        for r in range(k):
            if r == col or aug[r][col] == 0:
                continue
            factor = aug[r][col]
            for c in range(col, width):
                aug[r][c] ^= gf_mul(factor, aug[col][c])
    return [bytes(row[k:]) for row in aug]


def recover(k, m, available):
    """available: dict index -> bytes. Returns original data shards."""
    if len(available) < k:
        raise ValueError("INSUFFICIENT_SHARDS")
    indices = sorted(available)[:k]
    mat = []
    for idx in indices:
        if idx < k:
            row = [0] * k
            row[idx] = 1
        else:
            row = parity_row(k, idx - k)
        mat.append(row)
    rhs = [available[i] for i in indices]
    return gauss_solve(mat, rhs), indices


# -------------------------------------------------------------- digests

def shard_digest(index: int, data: bytes) -> str:
    h = hashlib.sha256()
    h.update(b"ec-shard-v1")
    h.update(struct.pack(">H", index))
    h.update(struct.pack(">Q", len(data)))
    h.update(data)
    return h.hexdigest()


def _tlv_string(tag, s: bytes):
    return bytes([tag]) + struct.pack(">H", len(s)) + s


def covered_encoding(fields: dict, shards_meta):
    out = b""
    out += bytes([0, fields["format_version"]])
    out += _tlv_string(1, fields["field_primitive"].encode())
    out += bytes([2, fields["k"]])
    out += bytes([3, fields["m"]])
    out += bytes([4]) + struct.pack(">I", fields["shard_len"])
    out += bytes([5]) + struct.pack(">Q", fields["original_len"])
    out += bytes([6]) + struct.pack(">Q", fields["pad_len"])
    out += _tlv_string(7, fields["digest_algorithm"].encode())
    out += _tlv_string(8, fields["object_id"].encode())
    out += bytes([9]) + struct.pack(">H", len(shards_meta))
    for index, digest_hex in shards_meta:
        digest = bytes.fromhex(digest_hex)
        out += struct.pack(">H", index)
        out += struct.pack(">I", len(digest))
        out += digest
    return out


def build_manifest(k, m, shard_len, original: bytes, shards, object_id):
    shards_meta = [(i, shard_digest(i, shards[i])) for i in range(k + m)]
    fields = {
        "format_version": 1,
        "field_primitive": "GF2P8-0x11B-G3",
        "k": k,
        "m": m,
        "shard_len": shard_len,
        "original_len": len(original),
        "pad_len": k * shard_len - len(original),
        "digest_algorithm": "SHA-256",
        "object_id": object_id,
    }
    tlv = covered_encoding(fields, shards_meta)
    fields.update({
        "shard_count": k + m,
        "shards": [{"index": i, "digest_hex": d} for i, d in shards_meta],
        "manifest_digest_hex": hashlib.sha256(tlv).hexdigest(),
        "covered_fields": [
            "format_version", "field_primitive", "k", "m", "shard_len",
            "original_len", "pad_len", "digest_algorithm", "object_id",
            "shard_digests",
        ],
    })
    return fields, tlv.hex()


# ------------------------------------------------------------- fixtures

def synthetic_original(seed_text: str, length: int) -> bytes:
    """Deterministic non-repetitive bytes from an independent PRNG
    (SHA-256 counter mode), so golden inputs are not hardcoded demos."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(f"{seed_text}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:length])


def emit_fixtures(path):
    fixtures = {"field": "GF(2^8) mod 0x11B, generator 3",
                "cauchy_known": {"k3_m2_row_p0": [hex(v) for v in parity_row(3, 0)]},
                "gf_kat": {"mul_0x57_0x83": hex(gf_mul(0x57, 0x83)),
                           "inv_0x53": hex(gf_inv(0x53)),
                           "mulbyx_128": hex(_mul_slow(128, 2))},
                "cases": []}
    for case_id, (k, m, length) in {
        "k3m2_l31": (3, 2, 31),     # non-aligned: 2 padding bytes
        "k3m2_l0": (3, 2, 0),       # empty object: shard_len floor of 1
        "k4m2_l40": (4, 2, 40),     # exactly aligned, 0 padding
    }.items():
        original = synthetic_original(case_id, length)
        shard_len, shards = encode(k, m, original)
        manifest, tlv_hex = build_manifest(
            k, m, shard_len, original, shards, f"golden-{case_id}")
        fixtures["cases"].append({
            "case_id": case_id,
            "k": k,
            "m": m,
            "original_b64": base64.b64encode(original).decode(),
            "shard_len": shard_len,
            "pad_len": k * shard_len - length,
            "shards_b64": [base64.b64encode(s).decode() for s in shards],
            "shard_digests_hex": [shard_digest(i, shards[i]) for i in range(k + m)],
            "covered_tlv_hex": tlv_hex,
            "manifest": manifest,
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fixtures, f, indent=2, sort_keys=True)
    print(f"wrote {path} with {len(fixtures['cases'])} golden cases", file=sys.stderr)


def recover_cli(manifest_path, shards_dir):
    """Standalone recovery: read the Rust-produced manifest + shard files and
    print recovered bytes (b64). Proves cross-implementation interop."""
    with open(manifest_path, "rb") as f:
        manifest = json.loads(f.read())
    k, m = manifest["k"], manifest["m"]
    available = {}
    for i in range(k + m):
        p = os.path.join(shards_dir, f"shard-{i:05d}.bin")
        if os.path.exists(p):
            available[i] = open(p, "rb").read()
    data_shards, used = recover(k, m, available)
    padded = b"".join(data_shards)
    original = padded[:manifest["original_len"]]
    print(json.dumps({"used": used, "original_b64":
                      base64.b64encode(original).decode()}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit")
    e.add_argument("--out", required=True)
    r = sub.add_parser("recover")
    r.add_argument("--manifest", required=True)
    r.add_argument("--shards", required=True)
    args = ap.parse_args()
    if args.cmd == "emit":
        emit_fixtures(args.out)
    else:
        recover_cli(args.manifest, args.shards)
