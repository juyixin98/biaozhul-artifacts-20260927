#!/usr/bin/env python3
"""
Independent reference implementation + test-vector generator for the
ec-service Reed-Solomon erasure code.

This file is intentionally independent from the Rust code under test:
- GF(2^8) arithmetic is implemented from scratch in Python using the
  Russian-peasant multiplication (no log/exp tables), so even a shared
  table-generation bug cannot make vectors agree.
- Systematic encoding matrices are derived by explicit Gauss-Jordan on an
  augmented copy (not by the Rust P*A^-1 shortcut), giving an independent
  derivation of the same construction.
- Output is deterministic JSON consumed verbatim by the Rust integration
  tests (tests/data/test_vectors.json).

Field: GF(2^8), primitive polynomial 0x11d (x^8+x^4+x^3+x^2+1), generator 2.
"""

import argparse
import hashlib
import itertools
import json


# ---------------------------------------------------------------- GF(2^8)

PP = 0x11D


def gf_mul(a: int, b: int) -> int:
    """Russian-peasant multiplication modulo 0x11d (independent of tables)."""
    result = 0
    aa, bb = a, b
    while bb:
        if bb & 1:
            result ^= aa
        high = aa & 0x80
        aa = (aa << 1) & 0xFF
        if high:
            aa ^= (PP & 0xFF)  # reduce; implicit x^8 bit is discarded by &0xFF
        bb >>= 1
    return result


def gf_inv(a: int) -> int:
    assert a != 0
    # brute-force inverse: fine for GF(256) and independent of Fermat trick
    for x in range(1, 256):
        if gf_mul(a, x) == 1:
            return x
    raise AssertionError("no inverse")


def gf_pow(a: int, n: int) -> int:
    r = 1
    for _ in range(n):
        r = gf_mul(r, a)
    return r


# --------------------------------------------------------- linear algebra

def mat_invert(a):
    """Gauss-Jordan inverse over GF(256); raises on singular matrix."""
    n = len(a)
    m = [row[:] + [1 if i == j else 0 for j in range(n)]
         for i, row in enumerate(a)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if m[r][col] != 0), None)
        if pivot is None:
            raise ValueError("singular")
        m[col], m[pivot] = m[pivot], m[col]
        p = m[col][col]
        pinv = gf_inv(p)
        m[col] = [gf_mul(x, pinv) for x in m[col]]
        for r in range(n):
            if r != col and m[r][col] != 0:
                f = m[r][col]
                m[r] = [x ^ gf_mul(f, y) for x, y in zip(m[r], m[col])]
    return [row[n:] for row in m]


def mat_vec(a, v):
    return [
        _xor_all(gf_mul(c, x) for c, x in zip(row, v))
        for row in a
    ]


def mat_mul(a, b):
    n, k, p = len(a), len(b), len(b[0])
    out = [[0] * p for _ in range(n)]
    for i in range(n):
        for j in range(p):
            out[i][j] = _xor_all(gf_mul(a[i][t], b[t][j]) for t in range(k))
    return out


def _xor_all(it):
    r = 0
    for x in it:
        r ^= x
    return r


# ------------------------------------------------------ systematic matrix

def vandermonde(k, m):
    """V[i][j] = (i+1)^j, shape (k+m) x k."""
    return [
        [gf_pow(i + 1, j) for j in range(k)]
        for i in range(k + m)
    ]


def _transpose(m):
    return [list(col) for col in zip(*m)]


def systematic_matrix(k, m):
    """
    Independent derivation of the systematic parity matrix C.

    We solve C * A = P  =>  A^T * C^T = P^T  for C^T by Gauss-Jordan on the
    augmented block [A^T | P^T] (k rows, k+m columns). Reducing A^T to I
    leaves C^T on the right; transposing gives C, and the full systematic
    matrix is [I ; C].

    This deliberately differs from the Rust implementation, which computes
    P * A^-1 directly with an explicit matrix multiply; only the result is
    shared, so an implementation-path bug in one cannot mask the other.
    """
    v = vandermonde(k, m)
    a_t = _transpose(v[:k])           # k x k
    p_t = _transpose(v[k:])           # k x m
    w = [a_t[i][:] + p_t[i][:] for i in range(k)]
    for col in range(k):
        pivot = next(r for r in range(col, k) if w[r][col] != 0)
        w[col], w[pivot] = w[pivot], w[col]
        pinv = gf_inv(w[col][col])
        w[col] = [gf_mul(x, pinv) for x in w[col]]
        for r in range(k):
            if r != col and w[r][col] != 0:
                f = w[r][col]
                w[r] = [x ^ gf_mul(f, y) for x, y in zip(w[r], w[col])]
    for i in range(k):
        assert w[i][:k] == [1 if j == i else 0 for j in range(k)]
    c_t = [row[k:] for row in w]      # k x m
    c = _transpose(c_t)               # m x k
    return [[1 if i == j else 0 for j in range(k)] for i in range(k)] + c


# ------------------------------------------------------------------ coding

def encode(data: bytes, k: int, m: int):
    shard_len = (len(data) + k - 1) // k
    padded = list(data) + [0] * (shard_len * k - len(data))
    data_shards = [padded[i * shard_len:(i + 1) * shard_len] for i in range(k)]
    em = systematic_matrix(k, m)
    shards = [s[:] for s in data_shards]
    for i in range(k, k + m):
        parity = []
        for t in range(shard_len):
            col = [data_shards[j][t] for j in range(k)]
            parity.append(_xor_all(gf_mul(em[i][j], col[j]) for j in range(k)))
        shards.append(parity)
    return shards, shard_len, len(padded) - len(data)


def reconstruct(available, k, m, shard_len):
    """available: list of (index, bytes); rebuilds all k+m shards."""
    assert len(available) >= k
    em = systematic_matrix(k, m)
    chosen = available[:k]
    a = [em[idx] for idx, _ in chosen]
    ai = mat_invert(a)
    data_shards = [[0] * shard_len for _ in range(k)]
    for t in range(shard_len):
        y = [b[t] for _, b in chosen]
        for i in range(k):
            data_shards[i][t] = _xor_all(gf_mul(ai[i][j], y[j]) for j in range(k))
    full = [s[:] for s in data_shards]
    for i in range(k, k + m):
        full.append([
            _xor_all(gf_mul(em[i][j], data_shards[j][t]) for j in range(k))
            for t in range(shard_len)
        ])
    return full


# --------------------------------------------------------------- vectors

CASES = [
    # (label, data hex, k, m) — small data for exhaustive erasure enumeration
    ("k2m1_exact2", b"\x00", 2, 1),
    ("k2m1_3bytes", b"abc", 2, 1),
    ("k1m1_5bytes", b"hello", 1, 1),
    ("k3m2_7bytes", bytes([0x00, 0x01, 0x02, 0x80, 0xff, 0x5a, 0xa5]), 3, 2),
    ("k4m2_9bytes", b"erasure!!", 4, 2),
    ("k2m1_empty", b"", 2, 1),
]


def erasure_sets(total, erasures):
    return sorted(itertools.combinations(range(total), erasures))


def build_vectors():
    out = {
        "generator": "scripts/gen_vectors.py (independent Python reference, "
                     "GF256 pp=0x11d; russian-peasant mul; augmented Gauss-Jordan)",
        "field": {"poly": "0x11d", "generator": 2},
        "gf_spot_checks": [],
        "cases": [],
    }

    # Hand-checkable field facts, independently recomputed here.
    checks = [
        ("2*0x80", gf_mul(2, 0x80)),
        ("3*3", gf_mul(3, 3)),
        ("16*16", gf_mul(0x10, 0x10)),
        ("2**255", gf_pow(2, 255)),
        ("inv(3)*3", gf_mul(gf_inv(3), 3)),
        ("53/53", gf_mul(gf_mul(0x53, 0x7e), gf_inv(0x53))),
    ]
    for name, val in checks:
        out["gf_spot_checks"].append({"name": name, "value": val})

    for label, data, k, m in CASES:
        shards, shard_len, pad_len = encode(data, k, m)
        em = systematic_matrix(k, m)
        total = k + m
        case = {
            "label": label,
            "k": k,
            "m": m,
            "data_hex": data.hex(),
            "original_len": len(data),
            "shard_len": shard_len,
            "pad_len": pad_len,
            "payload_sha256": hashlib.sha256(data).hexdigest(),
            "encoding_matrix": em,
            "shards_hex": [bytes(s).hex() for s in shards],
            # Enumerate EVERY recoverable erasure combination (1..=m shards
            # gone): for each, reconstruct from the complement and record the
            # exact byte-level expected result.
            "erasure_recoveries": [],
            # Exactly m+1 missing is NOT recoverable (expect failure category).
            "over_tolerance_example": None,
        }
        for n_missing in range(1, m + 1):
            for combo in erasure_sets(total, n_missing):
                avail = [(i, shards[i]) for i in range(total) if i not in combo]
                rebuilt = reconstruct(avail, k, m, shard_len)
                assert rebuilt == shards, (label, combo)
                case["erasure_recoveries"].append({
                    "missing": list(combo),
                    "available": [i for i in range(total) if i not in combo],
                    "rebuilt_shards_hex": [bytes(s).hex() for s in rebuilt],
                })
        if total >= m + 1 and k <= total:
            # m+1 erasures -> only k-1 available -> must fail.
            combo = list(range(m + 1))
            case["over_tolerance_example"] = {
                "missing": combo,
                "available_count": total - len(combo),
                "expected_error_code": "NOT_ENOUGH_SHARDS",
            }
        out["cases"].append(case)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/data/test_vectors.json")
    args = ap.parse_args()
    vectors = build_vectors()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(vectors, f, indent=2, sort_keys=True)
        f.write("\n")
    n_rec = sum(len(c["erasure_recoveries"]) for c in vectors["cases"])
    print(f"wrote {args.out}: {len(vectors['cases'])} cases, "
          f"{n_rec} recoverable erasure combinations")
    # Console spot checks for quick manual review.
    for c in vectors["gf_spot_checks"]:
        print(f"  {c['name']} = {c['value']}")


if __name__ == "__main__":
    main()
