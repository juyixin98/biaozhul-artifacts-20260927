# rangecode

Fixed-precision integer **range encoder / decoder** in Rust, with static
frequency tables, bounded adaptive rescaling, and a **chunked, CRC-protected
on-disk container**. Exposed through a CLI, an Axum HTTP validation service,
and a filesystem persistence adapter. All fixtures are local and synthetic —
no production accounts or real business data are needed.

## What is implemented

The behavior contract is enforced by code, not just demonstrated:

1. **Integer normalization and carry propagation.** Every operation is exact
   integer arithmetic:
   `low += (range/total)*cum`, `range = (range/total)*freq`. Carries propagate
   through a pending-`0xFF` byte chain (Subbotin/LZMA-style). **Zero-frequency
   symbols cannot be encoded** (`EncodeError::ZeroFrequency`).
   See [`src/range.rs`](src/range.rs).
2. **Bounded frequency totals; consistent update/rescale points.** Tables
   validate `1 <= total <= bound <= 2**24`. The adaptive model ceiling-halves
   counts atomically and requires `alphabet <= bound/2` headroom so every
   rescale stays bounded; a rescale publishes a new TABLE *epoch* at the exact
   point before the triggering symbol. See [`src/table.rs`](src/table.rs),
   [`src/rescale.rs`](src/rescale.rs).
3. **Decode output bounded by declared length and resource budgets.** The
   decoder reads exactly the declared number of symbols; header-declared
   counts and container byte length are checked against `Budgets` before
   allocation. Truncation reports the exact byte offset.
4. **Termination and byte order documented.** The stream starts with seed
   `0x00`, four big-endian code bytes, and finishes with five `ShiftLow`
   passes (the last forced flush resolves all pending bytes). All integers in
   the container are big-endian. See the module docs in
   [`src/range.rs`](src/range.rs) and [`src/container.rs`](src/container.rs).

### Module layout (real responsibilities, not a single file)

| Module | Responsibility |
|---|---|
| `src/table.rs` | validated frequency tables, cumulative intervals, inverse lookup |
| `src/range.rs` | the fixed-precision encode/decode kernel |
| `src/rescale.rs` | bounded adaptive counts and atomic rescale points |
| `src/format.rs` | binary constants, big-endian reader/writer, CRC-32 |
| `src/container.rs` | chunked framing, epochs, CRC checks, strict parse, budgets |
| `src/storage.rs` | filesystem adapter with atomic temp-file+rename writes |
| `src/diagnostics.rs` | request ids, accept/reject/indeterminate records, redaction |
| `src/config.rs` | defaults + JSON file + `RANGECODE_*` env overrides |
| `src/server.rs` | Axum HTTP validation interface |
| `src/bin/rangecode.rs` | CLI (`encode`/`decode`/`verify`/`inspect`/`serve`) |

## Prerequisites

* Rust (tested with 1.98): `rustc`/`cargo` on PATH.
* `python3` (3.8+) **only** for the independent cross-language test and
  regenerating golden vectors. The crate builds and 93 tests pass without it;
  the two Python tests self-skip if no interpreter is found.

## Quick start

```bash
cargo build --release

# encode (adaptive chunked container) then decode, round-trip
echo -n "hello range coder" > /tmp/in.txt
./target/release/rangecode encode --mode adaptive /tmp/in.txt /tmp/in.rcmp
./target/release/rangecode decode /tmp/in.rcmp /tmp/out.txt
cmp /tmp/in.txt /tmp/out.txt && echo "round-trip OK"

# inspect structure and verify a container
./target/release/rangecode inspect /tmp/in.rcmp
./target/release/rangecode verify /tmp/in.rcmp
```

Use the bundled samples (long repetition, alternation, full alphabet,
one-byte) and an explicit frequency table:

```bash
./target/release/rangecode encode --freq samples/freq-text.json \
    samples/repetitive.txt /tmp/rep.rcmp
./target/release/rangecode verify /tmp/rep.rcmp
```

## Run the tests

```bash
cargo test
```

Expected: **99 passing tests**, 0 warnings (`cargo clippy --all-targets` is
clean). This includes:

* 53 library unit tests (kernel interval math, tables, rescale, container,
  storage, diagnostics),
* `tests/golden_vectors.rs` — Rust vs. an **independent Python reference**,
  asserting byte-identical payloads and per-symbol `(low, range)` traces,
* `tests/failure_categories.rs` — exact error variants for truncation at
  every cut point and illegal frequency tables,
* `tests/roundtrip_shapes.rs` — empty, one-byte, long runs, alternating,
  full-alphabet, LCG-random, plus bidirectional Rust↔Python interop,
* `tests/fuzz_robustness.rs` — ~95k random/mutated inputs that must never
  panic, return an out-of-alphabet symbol, or allocate from declared counts,
* `tests/http_api.rs` — the Axum service driven in-process, including
  redaction (raw payloads never appear in diagnostics).

Regenerate the golden vectors (checked in) with:

```bash
python3 tools/generate_golden.py
```

## HTTP service

```bash
cargo run -- serve --config config/local-dev.json
# or: cargo run --release -- serve   (uses config/default.json values)
```

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + active bound/storage |
| POST | `/v1/encode` | `{data_base64, mode, frequencies?, id?, chunk_target?}` |
| POST | `/v1/decode` | `{container_base64}` |
| GET | `/v1/jobs` | list persisted job ids |
| GET | `/v1/jobs/:id` | decode a persisted job |

Example:

```bash
curl -s localhost:8080/health
B64=$(printf 'the quick brown fox' | base64)
curl -s -X POST localhost:8080/v1/encode \
  -H 'content-type: application/json' \
  -d "{\"data_base64\":\"$B64\",\"mode\":\"adaptive\",\"id\":\"demo\"}"
curl -s localhost:8080/v1/jobs/demo | python3 -m json.tool
```

Every response carries a `diagnostics` object with a **request id** (honored
from an inbound `X-Request-Id` header, otherwise generated), the decision
(`accepted` / `rejected` / `indeterminate`), a stable machine-readable
`error_kind`, key derived state, and a **fingerprint** of the input (length,
CRC, first/last 8 bytes in hex). Raw payloads are never logged; decoded data
is only returned when `expose_data` is enabled.

## Container format (big-endian)

```
header 28B  magic "RCMP" | u16 version=1 | u16 flags | u32 bound |
             u32 alphabet | u64 declared_symbols | u32 crc32(previous 24B)
frames ...  TABLE 'T' u32 len (u32 entries, u32[entries] freqs) u32 crc
             CHUNK 'C' u32 len (u64 start, u32 epoch, u32 n, u32 plen,
                                  range payload[plen]) u32 crc
             EOF   'E' u32 len (u64 final_symbol_count) u32 crc
```

The decoder is strict: bad magic/version/flags, CRC mismatches, unknown
epochs, out-of-order or overlapping chunks, count disagreements, trailing
bytes, and missing EOF each produce a distinct `ContainerError` variant.

## Configuration

Defaults live in `src/config.rs`; `config/default.json` and
`config/local-dev.json` are ready-to-use files. Any field is overridable by an
environment variable named `RANGECODE_<FIELD>` (e.g.
`RANGECODE_BIND_ADDR`, `RANGECODE_FREQUENCY_BOUND`). Env wins over file wins
over defaults. Invalid values fail fast at startup with a precise message.

## License

MIT OR Apache-2.0.
