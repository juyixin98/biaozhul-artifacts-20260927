# mphf — Immutable-key Minimal Perfect Hash Build/Query Service

A minimal perfect hash function (MPHF) over an **immutable, duplicate-free
set of byte-string keys**, built with a **BDZ-style 3-uniform hypergraph
peeling algorithm**, served over HTTP with Axum and persisted to the local
filesystem in a versioned, checksummed binary format.

- Every member key maps to a unique slot in `0..n` (a permutation — minimal
  and collision-free on the target set).
- Set membership is **never assumed from a slot hit**: out-of-set keys are
  rejected either by exact-key comparison or by a fingerprint.
- The on-disk format binds the **hash seed, algorithm id, and format
  version**; a CRC-32 guards every byte.
- All inputs are local synthetic fixtures; no production accounts or
  external services are involved.

## Layout / module responsibilities

| Path | Responsibility |
|---|---|
| `src/hash.rs` | Deterministic keyed 64-bit hash streams, BDZ edge `{h0,h1,h0+h1} mod m`, fingerprints. Exact integer contract (documented, mirrored by the Python oracle). |
| `src/kernel.rs` | Pure graph core: hypergraph peeling (with stale-queue guards) and reverse g-assignment. Edge-source agnostic, unit-tested with scripted hypergraphs. |
| `src/index.rs` | In-memory index: packed g table, occupancy bitset, word-level rank, slot mapping, membership probes (`accept / reject / undetermined` with named reasons). |
| `src/builder.rs` | Build pipeline: dedup, prime table sizing, seeded retry loop with a cap and per-attempt history, final bijection self-check. |
| `src/format.rs` | Versioned binary container: magic, version, algorithm, seed, n/m, CRC-32; strict decode with categorized errors. |
| `src/persistence.rs` | Filesystem adapter: safe set names, atomic temp-file+rename writes, in-memory cache. |
| `src/config.rs` | Build/server configuration (TOML) with validation. |
| `src/server.rs` | Axum HTTP verification interface; request ids; redacted key logging. |
| `src/error.rs` | Closed set of error categories (`ErrorKind`). |
| `src/bin/mphfd.rs` | CLI front-end (`serve`, `build`, `lookup`). |
| `tests/reference/mphf_ref.py` | **Independent from-scratch Python oracle** (no shared code) + fixture generator. |
| `tests/fixtures/golden.json` | Concrete expected hashes, edges, slots, and non-member decisions emitted by the Python oracle. |
| `tests/*.rs` | Independent test crates (see below). |
| `examples/` | `smoke.rs` end-to-end example and local synthetic key fixtures. |

## Algorithm summary

For a set of `n` keys, choose a prime vertex count `m ≈ n/load_factor`
(default load factor `0.75`; floor `m ≥ 31` for tiny sets). Each key defines
the 3-edge `{h0 mod m, h1 mod m, (h0+h1) mod m}` from two independent keyed
hash streams.

1. **Peel.** Repeatedly remove an edge incident on a degree-1 vertex. If a
   3-core remains, retry with another deterministic seed (cap
   `max_attempts`, default 256).
2. **Assign.** In reverse peel order, set
   `g[v] = (position − g[a] − g[b]) mod 3`, marking `v` occupied.
3. **Rank.** Occupied vertices of each g-class (0/1/2) are ranked with an
   O(1) word-cumulative rank structure. The slot of a key is
   `Σ_{j<g(v)}|G_j| + rank_{g(v)}(v)` where `v` is the selector-selected
   vertex. The occupied set guarantees exactly `n` valid slots.
4. **Verify.** A query maps to a candidate slot but is accepted only if the
   stored fingerprint matches (or the stored original key equals the query).
   Selectors landing on unoccupied vertices, or edges that cannot be formed
   under the bound seed, are rejected outright.

## Prerequisites

- Rust (edition 2021). Verified with `rustc 1.98.1`; any recent stable works.
- Python 3 (3.10+) **only** to regenerate the golden fixture / run the
  oracle. The Rust build and tests do not require Python.

Dependency versions are pinned in `Cargo.toml` / `Cargo.lock`
(axum 0.8, tokio 1, serde 1, crc32fast 1, thiserror 2, toml 0.8,
tracing 0.1).

## Build & test (from a clean checkout)

```bash
cargo build --release
cargo test                      # all Rust tests (unit + integration)
```

Independent oracle self-checks and fixture regeneration:

```bash
python3 tests/reference/mphf_ref.py verify --set-size 500 --seed 7
python3 tests/reference/mphf_ref.py golden --out tests/fixtures/golden.json
```

What the test commands assert (concrete results, not "interface callable"):

- `tests/kernel_golden.rs` — exact 64-bit hash/fingerprint vectors against
  Python constants; scripted disjoint graph peels; the solid K4³ hypergraph
  fails with exactly 4 edges remaining; empty graph peels with 0 steps.
- `tests/exhaustive.rs` — for every `n = 0..=12` across four seed namespaces
  and both verifier modes: members produce the exact permutation
  `{0,…,n−1}`; a deterministic non-member battery is never accepted; empty
  sets reject everything.
- `tests/golden.rs` — the Rust build picks the **same seed and m** as the
  Python oracle and reproduces every recorded member slot; oracle-recorded
  non-members are rejected with a named category.
- `tests/peeling_failure.rs` — forced solid-core on attempts 1–2 then
  success on attempt 3 through the real retry core (asserts attempt count,
  per-attempt history, `core_remain`/`peeled` labels); all-failing cap
  returns `peeling_failed`; invalid config returns `invalid_input`.
- `tests/persistence.rs` — reload preserves slots/membership; bit-flip →
  `checksum_mismatch`; bad magic → `format_magic`; bad version →
  `format_version`; truncation never loads; empty set round-trips; atomic
  writes leave no temp file.
- `tests/server_api.rs` — in-process Axum router tests: accept/reject
  decisions and reasons, request-id echo/generation, dedup counts, empty-set
  rejection, 400 for bad params/names/JSON, 404 `set_not_found`, health.

## Running the service

```bash
# CLI: build an index from a local fixture, then query it
cargo run --release -- build --set fruits --keys examples/keys/fruits.txt --bits 0 --data-dir data
cargo run --release -- lookup --set fruits --key banana  --data-dir data   # ACCEPT
cargo run --release -- lookup --set fruits --key durian  --data-dir data   # REJECT (exit 2)

# HTTP server
cp mphfd.example.toml mphfd.toml          # optional
cargo run --release -- serve --config mphfd.toml
```

### Request samples

Build (exact-key verification via `"fingerprint_bits": 0`):

```bash
curl -s -X POST 127.0.0.1:8080/sets \
  -H 'content-type: application/json' \
  -H 'x-request-id: demo-001' \
  -d '{"name":"fruits","keys":["apple","banana","cherry"],"fingerprint_bits":0}'
```

Member probe:

```bash
curl -s -X POST 127.0.0.1:8080/sets/fruits/lookup \
  -H 'content-type: application/json' \
  -H 'x-request-id: demo-002' \
  -d '{"key":"banana"}'
# {"request_id":"demo-002","member":true,"decision":"accept",
#  "reason":"verifier_match","slot":1,"key":"len=6,tag=...", ...}
```

Non-member probe (rejected, not a false member):

```bash
curl -s -X POST 127.0.0.1:8080/sets/fruits/lookup \
  -H 'content-type: application/json' -d '{"key":"durian"}'
# {"member":false,"decision":"reject",
#  "reason":"key_mismatch" | "unoccupied_vertex" | "edge_collision", ...}
```

Other endpoints: `GET /healthz`, `GET /sets`, and the same lookup via
`GET /sets/{name}/lookup?key=...`. Binary keys may be sent with
`"key_base64"` / `"keys_base64"` (standard or URL-safe alphabet).

### Diagnostics & sensitive data

Every response carries `request_id` (echoed from `x-request-id` or minted).
Structured logs state **why** a request was accepted, rejected, or could not
be decided, with set name, candidate slot, and a one-way key descriptor
`len=<n>,tag=<8 hex chars of a keyed hash>` — raw key bytes are never logged.

## On-disk format (version 1, algorithm id 1 = BDZ-3)

```
 0   4  magic "MPHF"
 4   1  FORMAT_VERSION = 1
 5   1  algorithm id (1)
 6   1  verifier bits (0 = exact keys, else 8/16/32/64)
 7   1  reserved (0)
 8   8  n
16   8  m
24   8  seed
32   8  data_len
40   4  CRC-32 over header[8..40] ++ body
44   4  reserved (0)
48  ..  body = g_packed(ceil(m/4)) ++ occupancy(ceil(m/8))
             ++ fingerprints[n*bits/8]            (fingerprint mode)
             ++ key_blob ++ (n+1) LE u64 offsets  (exact-key mode)
```

## False-positive tradeoff

Exact-key mode (`bits=0`) never accepts a non-member. Fingerprint modes are
space-efficient with FPR `2^-bits` per distinct non-member (unoccupied /
edge-collision rejections have zero FPR regardless).
