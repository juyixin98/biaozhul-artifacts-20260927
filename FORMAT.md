# HUFF-CANONICAL container format (v1)

Authoritative wire specification for the `.hfc` container produced by this
repository. All integers are **little-endian**. All checksums are IEEE
**CRC-32** (poly `0xEDB88320`, init/xorout `0xFFFFFFFF`).

## 1. File layout

```
offset 0                32
┌──────────────────────────┐
│ header (32 bytes)        │
├──────────────────────────┤ 32
│ block frame 0            │  payload_len:u32 │ payload_crc:u32 │ payload
│ block frame 1 …          │
├──────────────────────────┤ dir_offset
│ block directory          │  16 bytes per block
├──────────────────────────┤ dir_offset + dir_len
│ footer (16 bytes)        │
└──────────────────────────┘
```

The file ends exactly at `dir_offset + dir_len + 16`. Any trailing byte is
rejected (`TRAILING_DATA`).

### 1.1 Header (32 bytes)

| off | len | field           | notes                                            |
|-----|-----|-----------------|--------------------------------------------------|
| 0   | 4   | magic           | ASCII `HUFF` (`48 55 46 46`)                     |
| 4   | 1   | version         | must be `1`; anything else → `UNKNOWN_VERSION`   |
| 5   | 1   | flags           | reserved, must be `0` → else `UNKNOWN_FLAGS`     |
| 6   | 4   | block_size      | 1..=1 048 576 (`0x100000`); else `BAD_BLOCK_SIZE`|
| 10  | 4   | original_total  | sum of the blocks' original lengths              |
| 14  | 4   | block_count     | ≥ 1 (empty input still has one empty block)      |
| 18  | 4   | dir_offset      | byte offset of the directory                     |
| 22  | 4   | dir_len         | `block_count * 16`                               |
| 26  | 2   | reserved        | must be zero                                     |
| 28  | 4   | header_crc      | CRC-32 over bytes 0..28                          |

### 1.2 Block frame

| off | len | field       |
|-----|-----|-------------|
| 0   | 4   | payload_len |
| 4   | 4   | payload_crc |
| 8   | N   | payload     |

`payload_crc = CRC-32(payload)`. Frames are stored back-to-back, immediately
after the header; the directory must agree with the frame offsets
(`DIRECTORY_NOT_CONTIGUOUS` otherwise).

### 1.3 Directory entry (16 bytes each)

| off | len | field         |
|-----|-----|---------------|
| 0   | 4   | original_len  |
| 4   | 4   | payload_len   |
| 8   | 4   | frame_offset  |
| 12  | 4   | original_crc  |

`original_crc = CRC-32(decoded block bytes)`. The directory itself is
protected by `dir_crc` in the footer, and the footer repeats `dir_offset`,
`dir_len`, `block_count`; any disagreement is `DIRECTORY_BOUNDS_INVALID`.

### 1.4 Footer (16 bytes)

`dir_crc:u32`, `dir_offset:u32`, `dir_len:u32`, `block_count:u32`.

## 2. Block payload

| off | len | field          |
|-----|-----|----------------|
| 0   | 4   | original_len   |
| 4   | 4   | bits_total     |
| 8   | 2   | symbol_count   |
| 10  | 1   | sole_symbol    |
| 11  | 1   | reserved (0)   |
| 12  | 256 | code lengths   |
| 268 | N   | bitstream body |

`bits_total` is the number of **valid** bits in the body. The body occupies
`ceil(bits_total/8)` bytes; the unused low bits of the final byte must all be
zero (`INVALID_PADDING`). `bits_total` may not exceed the body's bit capacity
(`BITSTREAM_TRUNCATED`).

## 3. Canonical Huffman code (normative)

### 3.1 Tree construction and the tie-break rule

Frequencies are counts over the 256-symbol byte alphabet. A binary priority
queue holds `(weight, height, id)` tuples ordered ascending:

1. **weight** (symbol frequency),
2. **height** — leaves have height 0, an internal node has
   `1 + max(child heights)`,
3. **id** — leaves are numbered in ascending **symbol value** (0..255);
   internal nodes receive ids in creation order (256, 257, …).

The two smallest entries are popped together and their parent is pushed back,
until one node remains. This fully determines code lengths even when all
frequencies are equal, so encoding is deterministic across implementations.

### 3.2 Canonical code assignment

Symbols are ordered by `(code length ascending, symbol value ascending)`.
The first code is `0`; when the length increases by `d`, the running code is
`code = (code + 1) << d` (equivalently the INFLATE recurrence
`code = (code + count[len-1]) << 1` applied at every length, including empty
levels). Codes are packed MSB-first (the first bit of a codeword is the most
significant bit).

Maximum code length is **32** bits; deeper trees are rejected with
`CODE_LEN_TOO_LONG` at encode time.

### 3.3 Degenerate alphabets

* **Empty input** — one block with `original_len = 0`, `symbol_count = 0`,
  all 256 code lengths zero, empty body, `bits_total = 0`.
* **One distinct symbol** — `symbol_count = 1`, all code lengths zero, the
  symbol byte stored explicitly in `sole_symbol`, `bits_total = 0`. Decoding
  emits exactly `original_len` copies without reading any bit.
* **≥ 2 symbols** — every present symbol has a positive code length. A single
  positive length (`symbol_count = 1`, one length > 0) is an incomplete code
  and is rejected with `TABLE_INCOMPLETE`.

### 3.4 Decoder-side validation

The decoder rebuilds the table purely from the 256 stored lengths and
`symbol_count`:

* number of positive lengths must equal `symbol_count` (`BAD_SYMBOL_COUNT`;
  `symbol_count` ∈ 0..=256);
* each length ≤ 32 (`CODE_LEN_TOO_LONG`);
* Kraft sum `Σ 2^(32 − len) ≤ 2^32`, in exact 128-bit arithmetic
  (`TABLE_KRAFT_OVERFLOW` — the oversubscribed-tree case);
* incomplete multi-symbol tables (Kraft sum < 1) are accepted, like DEFLATE;
  an unassigned prefix then fails with `INVALID_CODEWORD`.

### 3.5 Bounded decoding

The decoder emits **exactly** `original_len` symbols. Reading past the
declared valid bits → `TRUNCATED_CODEWORD`; after the last symbol every valid
bit must have been consumed, padding bits must be zero, and the produced
length must equal `original_len` (`OUTPUT_LENGTH_MISMATCH`). A decoder may
never read past the frame or emit past the declared length.

## 4. Error codes (§9 reference)

Stable machine codes (see `HuffError::code`):

`BAD_MAGIC`, `UNKNOWN_VERSION`, `HEADER_TRUNCATED`, `HEADER_CRC_MISMATCH`,
`BLOCK_FRAME_TRUNCATED`, `BLOCK_CRC_MISMATCH`, `DIRECTORY_TRUNCATED`,
`DIRECTORY_CRC_MISMATCH`, `DIRECTORY_BOUNDS_INVALID`,
`DIRECTORY_NOT_CONTIGUOUS`, `TOTAL_LENGTH_MISMATCH`, `TRAILING_DATA`,
`BAD_BLOCK_SIZE`, `BLOCK_PAYLOAD_TRUNCATED`, `BLOCK_PAYLOAD_LENGTH_MISMATCH`,
`CODE_LEN_TOO_LONG`, `BAD_SYMBOL_COUNT`, `TABLE_SYMBOL_SHAPE_INVALID`,
`TABLE_KRAFT_OVERFLOW`, `TABLE_INCOMPLETE`, `BITSTREAM_TRUNCATED`,
`TRUNCATED_CODEWORD`, `INVALID_CODEWORD`, `INVALID_PADDING`,
`OUTPUT_LENGTH_MISMATCH`, `OUTPUT_TOO_LONG`, `UNKNOWN_FLAGS`.

Unknown/exceptional states are always reported with their real code; nothing
is collapsed into success.

## 5. Validation check order

The `/v1/validate` endpoint records an ordered, per-step verdict:

`header_present → magic → version → flags → header_crc → structure →
block_crc → semantic_decode` (which includes original CRC and total length).
Overall `ok` is true only when every step passes.
