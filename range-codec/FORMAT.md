# RC01 — wire format & integer specification

This document is the normative description of the fixed-precision range
coder kernel and the chunked container. All arithmetic is **integer**;
there is no floating point anywhere in the codec.

## 1. Range coder kernel

### 1.1 Constants

| name | value | meaning |
|------|-------|---------|
| `TOP` | `2^24` | renormalisation threshold |
| `BOT` | `2^16` | lowest width reachable between symbol steps |
| `MAX_FREQ_TOTAL` | `2^14 = 16384` | maximum sum of frequencies |
| `MAX_SYMBOLS` | `256` | maximum alphabet size of one table |
| `TAIL_SHIFTS` | `4` | tail shifts emitted after the final symbol |

State:

* `low` — lower interval bound, held in an unsigned 64-bit slot but always
  `< 2^33`;
* `range` — interval width, unsigned 32-bit, always `>= TOP` immediately
  before a symbol is coded.

Because `range >= 2^24` and `total <= 2^14`, the scaled unit
`step = range / total` is always `>= 2^10` — no sub-unit rounding or
underflow handling is needed inside a symbol step.

### 1.2 Frequency tables

A table is a vector of per-symbol non-negative integer frequencies `f[s]`.

* alphabet size `n` satisfies `1 <= n <= 256`;
* total `T = sum f[s]` satisfies `1 <= T <= 16384` — a zero total is
  rejected (`FREQ_TOTAL_OUT_OF_BOUNDS`) and so is any running prefix sum that
  crosses 16384;
* **a symbol with `f[s] = 0` cannot be encoded** (`ZERO_FREQUENCY_SYMBOL`).
  Its cumulative slot has zero width, so an inverse lookup can never land in
  it; on a corrupt stream that nevertheless resolves there the decoder
  returns `CODE_OUTSIDE_RANGE`.

Cumulative counts are prefix sums: `cum[0] = 0`,
`cum[s+1] = cum[s] + f[s]`, so `cum[n] = T`.

### 1.3 Normalisation and carry propagation

**Before coding each symbol**, while `range < TOP`, one shift step runs:

```
val   = low >> 24                 # integer, range 0..=0x1FF
carry = val >> 8                 # always exactly 0 or 1
emit  = val & 0xFF
append emit to the byte buffer
if carry == 1:
    walk backwards over previously emitted bytes:
        while byte == 0xFF: byte = 0x00; continue
        increment the first byte that is not 0xFF        # terminates at the sentinel
low   = (low << 8) mod 2^32
range = range << 8
```

The byte buffer is initialised with one permanent **carry sentinel** byte
`0x00` at index 0. The backwards carry walk always terminates there. Under
the interval invariant the sentinel only ever becomes `0x01`; it cannot
reach `0x02` (that would require the entire accumulated prefix to exceed
`2^32`, which the bounds forbid). A decoder therefore accepts exactly `0x00`
or `0x01` as its first byte and rejects anything else as
`INVALID_INIT_BYTE`.

This is ordinary carry propagation expressed purely as integer byte
operations — every emitted byte, carry and increment is observable in the
encoder trace (`EncoderStep.emitted`) and compared bit-for-bit against the
Python oracle.

### 1.4 Coding one symbol

Given the symbol `s` with `(cum[s], f[s])` and total `T`:

```
step  = range // T                         # integer division
low   = low + step * cum[s]
range = step * f[s]
# then renormalise lazily before the next symbol
```

The encoder calls `update(s)` on its model **immediately after** the symbol
is consumed; the decoder calls `update` with the symbol it resolved to at
the exact same point. This keeps the two tables in lockstep for the
adaptive model.

### 1.5 Decoder

Preamble: consume the sentinel byte (must be `0`/`1`), then read four bytes
**big-endian** into the 32-bit `code`. Initial `range = TOP` (so the first
normalisation is a no-op for the widest case).

Before each symbol, while `range < TOP`:

```
code  = (code << 8) | read_byte()         # big-endian byte order
range = range << 8
```

Then:

```
step   = range // T
scaled = code // step
s      = unique symbol with cum[s] <= scaled < cum[s] + f[s]
code   = code - step * cum[s]
range  = step * f[s]
```

`scaled >= T` is `CODE_OUTSIDE_RANGE`. Running out of renormalisation bytes
is `TRUNCATED` (with `needed`/`available`), never a panic or read past the
buffer.

### 1.6 Tail convergence

After the final symbol the encoder performs `TAIL_SHIFTS = 4` shift steps
without touching `range`. Why four: after the last narrowing, `low < 2^33`.
The first shift publishes byte 3 (resolving the possible carry into earlier
bytes); shifts 2–4 publish bytes 2,1,0 of the 32-bit window. After those
four shifts every bit the decoder's preamble and renormalisation can read
has been pinned, so the final symbol is uniquely decodable. The stream is
exactly long enough: the decoder consumes `5 + N` bytes where `N` is the
number of renormalisation shifts, and the encoder emits the sentinel plus
`N + 4` bytes.

### 1.7 Byte order

All multi-byte integers on the wire — the decoder's 32-bit code, every
container field, every frequency — are stored **big-endian (network byte
order)**. Renormalisation feeds new bytes into the low octet of `code`, so
the stream is read high byte first.

## 2. Models

### 2.1 Static

The table is fixed for the chunk and carried verbatim in the container.
Validation (alphabet size, total bound) happens before any kernel work.

### 2.2 Adaptive (integer rescale)

Every symbol starts at frequency `1`. After a symbol is observed its count
is incremented. The update/rescale point is identical for encoder and
decoder:

1. `f[s] += 1`; if `total <= MAX_FREQ_TOTAL`, rebuild cumulative counts and
   stop;
2. otherwise **rescale by integer rules**: `f[x] = max(1, f[x] // 2)` for
   every symbol, using integer floor division, then rebuild cumulatives.

The floor keeps every arithmetic value integral; `max(1, …)` guarantees any
symbol that was ever codeable remains codeable after rescaling (a frequency
can never decay to zero). The new total is strictly below the bound because
each non-zero count loses at least one in the halving. Each chunk starts a
fresh uniform model independently.

## 3. RC01 container

### 3.1 Header (20 bytes)

| offset | size | field |
|--------|------|-------|
| 0 | 4 | magic `"RC01"` (`52 43 30 31`) |
| 4 | 1 | version, must be `1` |
| 5 | 1 | flags: bit 0 = adaptive; bits 1–7 reserved, must be zero |
| 6 | 2 | `num_symbols`, big-endian, `1..=256` |
| 8 | 8 | `declared_len` total decoded symbol count, big-endian |
| 16 | 4 | `num_chunks` data-chunk count, big-endian |

### 3.2 Data chunk

| size | field |
|------|-------|
| 1 | tag `0xD0` |
| 4 | `sym_count`, big-endian, `1..=65535` |
| 4 | `payload_len`, big-endian |
| `4*num_symbols` | static mode only: one big-endian u32 frequency per symbol |
| `payload_len` | range-coded chunk (sentinel + code + tail) |
| 4 | CRC-32 over **tag through end of payload**, big-endian |

Adaptive chunks omit the frequency block (the uniform start is implicit).

### 3.3 End marker

Exactly one byte `0x45` (`'E'`). Empty input is `num_chunks = 0` followed
immediately by the end marker. Any bytes after it are `TRAILING_BYTES`.

### 3.4 CRC

CRC-32/ISO-HDLC: reflected polynomial `0xEDB88320`, init and xor-out
`0xFFFFFFFF`. Matches `zlib.crc32`. The checksum is verified **before** the
kernel touches the payload, so a corrupted chunk never enters arithmetic.

## 4. Resource budgets

The decoder enforces budgets from header values before allocating or
coding:

| budget | default | violation |
|--------|---------|-----------|
| `max_symbol_count` (`declared_len`) | `2^24` | `LENGTH_BUDGET_EXCEEDED` (HTTP 413) |
| `max_chunks` | 4096 | `CHUNK_BUDGET_EXCEEDED` (413) |
| `max_payload_bytes` (running sum) | 64 MiB | `BYTE_BUDGET_EXCEEDED` (413) |
| `max_chunk_symbols` | 65535 | `BAD_CHUNK_LENGTH` (422) |

The header's `num_chunks * max_chunk_symbols` upper bound is also checked
against `max_symbol_count` up front, so a hostile header cannot promise more
work than the budget permits even before chunk lengths are read.

## 5. Decisions and error codes

Every failure is either:

* **rejected** — definitively malformed (truncation, bad magic, CRC,
  illegal table, zero-frequency symbol, length mismatch, …); or
* **indeterminate** — framing parses but this build cannot interpret it
  (`RESERVED_FLAG`). A newer reader might accept it, so it is not called
  corrupt.

Stable codes appear in `CodecError::code()` and in HTTP `error.code`:
`ZERO_FREQUENCY_SYMBOL`, `SYMBOL_OUT_OF_RANGE`, `BAD_ALPHABET_SIZE`,
`FREQ_TOTAL_OUT_OF_BOUNDS`, `TRUNCATED`, `TRAILING_BYTES`, `BAD_MAGIC`,
`UNSUPPORTED_VERSION`, `RESERVED_FLAG`, `LENGTH_BUDGET_EXCEEDED`,
`LENGTH_TOO_LARGE`, `LENGTH_MISMATCH`, `BYTE_BUDGET_EXCEEDED`,
`CHUNK_BUDGET_EXCEEDED`, `UNKNOWN_CHUNK_TYPE`, `CRC_MISMATCH`,
`BAD_CHUNK_LENGTH`, `CODE_OUTSIDE_RANGE`, `INVALID_INIT_BYTE`,
`DECODER_EXHAUSTED`, `OUTPUT_LIMIT_EXCEEDED`, `CARRY_OVERFLOW`, `IO_ERROR`,
`STORED_ARTIFACT`, `NOT_FOUND`, `BAD_REQUEST`, `PAYLOAD_TOO_LARGE`,
`UNSUPPORTED_MEDIA_TYPE`.
