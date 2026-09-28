# Architecture and contracts

## Data flow

```
create request (b64/hex bytes)
        │  service.rs: validate name/encoding/size ──► Error (typed)
        ▼
  raw text Vec<u8>
        │  coding::code_with_sentinel        symbols: 0=$ , 1..=256=bytes
        ▼
  suffix::build_sa        prefix doubling, counting-sort radix, O(n log n)
        ▼
  bwt::build_bwt          L[i] = T[SA[i]-1] ($ if SA[i]=0)
  bwt::build_c_table      C[c] = # symbols < c
        ▼
  rank::OccTable          BWT + cumulative count snapshot every 256 rows
        ▼
  FmIndex                 + SA samples at rows 0,K,2K,…
        │  persistence::write_index_file   magic+header, CRC'd sections
        ▼
  {data}/{name}.fm  +  catalog.json (atomic tmp+rename writes)

search request
        ▼
  FmIndex::interval  backwards right-to-left updates on [lo,hi)
        ▼
  FmIndex::locate_row  LF walk + SA sample (terminates at $-L row)
        ▼
  sorted positions + per-result note; naive_scan only on /verify
```

## Cross-module contracts

* **Coding** (`coding.rs`) is the only place that maps bytes↔symbols. The
  invariant "exactly one sentinel, at the end, symbol 0 never produced by a
  byte" is re-checked in `build_sa` and again when loading a file.
* **Suffix array** (`suffix.rs`) returns a permutation of `0..n` with
  `sa[0] = n-1`; malformed inputs (internal sentinel, missing terminal
  sentinel) are `Error::Invariant`. Lengths are bounded to fit `u32` SA
  entries (`MAX_TEXT_LEN`).
* **Rank** (`rank.rs`) defines occurrence as **exclusive**:
  `occ(c,i) = |{j<i : BWT[j]=c}|`. Endpoints past the BWT or symbols outside
  the alphabet panic in debug (core programming error), never appear in API
  input paths.
* **Core** (`fm.rs`) never returns offsets outside `0..=text_len`. The
  debug self-check localizes *every* row and asserts the resolved positions
  are exactly the permutation `0..n`, for every build.
* **Persistence** (`persistence.rs`) is the only module doing I/O. It
  re-derives C and occ from stored bytes and **rebuilds the suffix array on
  load** to cross-check every stored SA sample, so a logically tampered file
  is rejected even when its CRCs are intact.
* **Service** (`service.rs`) is the sole boundary that maps `Error` to HTTP
  statuses and JSON codes.

## Error taxonomy (`error.rs`)

| Variant | category | code | HTTP |
|---|---|---|---|
| `EmptyText`, `BadBase64`, `BadName`, `BadSampleInterval`, `MissingField`, `BadEncoding` | invalid_input | `empty_text`, `bad_base64`, `bad_name`, `bad_sample_interval`, `missing_field`, `bad_encoding` | 400 |
| `TextTooLarge` | resource_exhausted | `text_too_large` | 413 |
| `NotFound`, `AlreadyExists` | state_conflict | `index_not_found`, `index_already_exists` | 404 / 409 |
| `PersistenceCorrupt{section,detail}`, `CatalogCorrupt`, `Invariant`, `Io` | compute_failure | `persistence_corrupt`, `catalog_corrupt`, `internal_invariant`, `io_error` | 500 |

`PersistenceCorrupt.section` names the layer that failed:
`magic | header | container | TXT1 | BWT1 | SAS1 (sa_samples)`.

## On-disk format (`FMIDX001`)

Little-endian throughout.

```
magic           8 bytes  b"FMIDX001"
name_len        u32
name            name_len bytes  ([A-Za-z0-9_-]{1,64})
sample_interval u32
section TXT1:  tag(4) payload_len(u32) crc32(u32) payload(raw text)
section BWT1:  ... u64 n, n*u16 symbols
section SAS1:  ... u64 count, count*u32 sampled SA values
```

Exactly these three sections, exactly this order; trailing bytes are
rejected. Each payload is independently CRC-32 protected, so flipping one
payload byte identifies the affected section. Writes go to `name.fm.tmp`
then `rename(2)`; `catalog.json` is rewritten the same way after the index
file lands, so a crash never leaves a catalog entry pointing at a
half-written file.

## Localization correctness

`LF(r) = C[L[r]] + occ(L[r], r)` maps the suffix at text position `p` to the
suffix at position `p-1`. From a result row we walk LF counting `steps`; at
a sampled row with stored value `SA[r']` the original position is
`SA[r'] + steps`. The unique row whose L symbol is the sentinel is the
suffix at position 0, so reaching it returns `steps`. With dense samples at
every K-th row the walk reaches a sample within K steps for any starting
row — exercised directly in `tests/sampling.rs`, which asserts that
localizing all rows yields each of `0..n` exactly once, for K = 1..=8.

## Request log (replay)

When `FM_LOG_DIR` is set, every request appends one JSON line to
`requests.jsonl`: run id, timestamp, method, path, status, duration,
index name, last searched `[lo,hi)`, total hit count, pattern count, and on
failure the error category and code. The run id is also the value of the
`x-run-id` response header, so a user-reported id finds one exact line with
enough intermediate state to reproduce the decision.
