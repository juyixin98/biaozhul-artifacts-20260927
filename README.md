# FM-Index Byte-Text Service

A pure-backend service that builds and serves an **FM-index** over arbitrary
byte strings — Burrows-Wheeler transform, a block-sampled rank/occurrence
structure, and sampled suffix-array localization — exposed as an Axum HTTP
API with a file-system persistence layer.

Everything is local and synthetic: no cloud accounts, no business data.
Fixtures are generated deterministically; test oracles are an independent
naive text scan.

## Quick start

```bash
cargo test                                   # 18 unit + 25 integration tests
cargo run --bin fm-make-fixtures             # (re)generate samples/*.json
cargo run                                    # start on 127.0.0.1:8080
```

Configure via environment variables (see [`.env.example`](.env.example)):
`FM_DATA_DIR`, `FM_BIND`, `FM_MAX_TEXT_BYTES`, `FM_DEFAULT_SAMPLE_INTERVAL`,
`FM_LOG_DIR`, `RUST_LOG`.

### 30-second tour

```bash
# create an index (text is standard base64; sample_interval optional)
curl -sS -X POST localhost:8080/indexes -H 'content-type: application/json' \
  -d "{\"name\":\"demo\",\"text\":\"$(printf banana | base64)\",\"sample_interval\":3}"

# search one or many patterns at once; overlapping matches are all returned
curl -sS -X POST localhost:8080/indexes/demo/search \
  -H 'content-type: application/json' \
  -d "{\"patterns\":[\"$(printf ana | base64)\",\"\",\"$(printf toolong | base64)\"]}"

# compare the index against an independent exhaustive scan, server-side
curl -sS -X POST localhost:8080/indexes/demo/verify \
  -H 'content-type: application/json' -d "{\"patterns\":[\"$(printf ana|base64)\"]}"

# inspect BWT symbols, C-table entries and sampling layout
curl -sS localhost:8080/indexes/demo/bwt
```

Binary payloads (including zero bytes) can be sent as `"encoding":"hex"`
instead of base64:

```bash
curl -sS -X POST localhost:8080/indexes -H 'content-type: application/json' \
  -d '{"name":"bin","text":"0000ff00","encoding":"hex"}'
```

## Defined semantics

| Case | Behavior |
|---|---|
| Byte `0x00` in text | Normal data. The sentinel is a distinct 257th symbol `0`; text bytes are coded as `byte+1`, so they can never collide with it. |
| Backwards search | Returns the **half-open** interval `[lo, hi)` over BWT rows; `count = hi - lo`. |
| Localization | Each result row is resolved via repeated LF mapping until a sampled row (or the unique sentinel-L row, whose suffix position is 0). |
| Empty pattern `""` | Matches every suffix boundary: interval `[0, n)`, positions `0..=text_len`. |
| Pattern longer than the text | Empty interval `[0, 0)`, `count 0`, HTTP 200 (not an error). |
| Empty text at create | `400 invalid_input / empty_text`. |
| Text over `FM_MAX_TEXT_BYTES` | `413 resource_exhausted / text_too_large`. |

## HTTP surface

| Method & path | Purpose |
|---|---|
| `GET /health` | liveness |
| `POST /indexes` | create `{name, text, encoding?, sample_interval?}` → 201 |
| `GET /indexes` | list metadata |
| `GET /indexes/{name}` | one index's metadata |
| `DELETE /indexes/{name}` | delete file + catalog record |
| `POST /indexes/{name}/search` | `{pattern}` **or** `{patterns:[...]}` + `encoding?` |
| `POST /indexes/{name}/verify` | like search, but each result also carries the naive-scan positions and an `agree` verdict with a human-readable `reason` |
| `GET /indexes/{name}/bwt` | BWT symbols, sentinel row, nonzero C-table entries, sample count |

Every response carries an `x-run-id` header (you may supply your own for
correlation; otherwise a UUID v4 is generated). Error bodies are uniform:

```json
{ "category": "state_conflict", "code": "index_not_found",
  "message": "index \"nope\" does not exist", "run_id": "…" }
```

The four categories are distinct and test-asserted:
`invalid_input` (400), `resource_exhausted` (413), `state_conflict`
(404/409), `compute_failure` (500 — including every persistence
corruption, with code `persistence_corrupt` / `catalog_corrupt` /
`io_error`).

## Layout

```
src/
  coding.rs        sentinel + 257-symbol byte alphabet (unique by construction)
  suffix.rs        suffix array, prefix doubling + radix sort, O(n log n)
  bwt.rs           BWT and the C table
  rank.rs          block-snapshot occurrence structure (occ(c,i), exclusive)
  fm.rs            index core: LF, half-open backwards search, SA-sample locate
  reference.rs     independent naive overlap-preserving scan (oracle / verify)
  persistence.rs   FMIDX001 binary sections (CRC-32 each), catalog.json
  service.rs       Axum routes, error taxonomy, run-id middleware
  config.rs        environment configuration
  logging.rs       JSONL request records for replay
  bin/fixtures.rs  deterministic synthetic fixture generator
tests/             integration suites + shared common/ helpers
samples/           generated, checked-in synthetic texts (base64 JSON)
docs/              ARCHITECTURE.md, TESTING.md
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the data/error
contracts between these modules and [`docs/TESTING.md`](docs/TESTING.md) for
the test catalog with real commands and output.
