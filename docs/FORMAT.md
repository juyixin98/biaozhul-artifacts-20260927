# Range-coded container format — version 1

All integers are **big-endian** (network byte order). The range-coded payload
itself is also emitted most-significant-byte first.

## 1. Kernel stream (inside one CHUNK)

Fixed-precision 32-bit arithmetic, byte-at-a-time normalization.

Constants:

* `TOP = 2**24`
* `INIT_RANGE = 0xFFFFFFFF`

Encoding one symbol with cumulative interval `[cum, cum+freq)` and table
total `total`:

```
low   += (range / total) * cum       # integer division
range  = (range / total) * freq
while range < TOP:
    range <<= 8
    ShiftLow()
```

`low` is maintained as a 33-bit value in a wider word: bit 32 is the carry.
`ShiftLow` resolves the top byte through a pending-byte chain:

* the stream keeps one anchor byte (`cache`, the last byte strictly below
  `0xFF`) plus `cache_size - 1` follower bytes awaiting resolution;
* when no carry fires, followers are `0xFF`; when a carry fires, the anchor
  is incremented (wrapping `0xFF -> 0x00` into the byte above) and followers
  become `0x00`.

Stream framing:

1. One **seed byte** `0x00` (the initial cache). The decoder rejects any
   other leading byte with `InvalidLeadingByte`.
2. Four initial code bytes read by the decoder (big-endian `code`).
3. Zero or more normalization bytes, produced at exactly the same points on
   encode and decode.
4. Five final `ShiftLow` passes after the last symbol. The first four flush
   the remaining 32 bits of `low`; the fifth is a forced flush whose digit is
   `0x00`, which deterministically resolves every pending follower.

A legal stream is consumed exactly by the decoder after the declared number
of symbols. Truncation is reported at the precise byte offset where the read
failed.

### Declared length is a trust boundary

Arithmetic coding without an explicit end-of-stream sentinel cannot, in
general, distinguish nested intervals: for a two-symbol equal-frequency
table, the sequences `[1]`, `[1,0]` and `[1,0,0]` all code to the same five
bytes `00 7F FF FF FF`. This is the same position LZMA takes: the **symbol
count is supplied by the framing layer**. The container binds that count to
CRC-protected frame and header fields, so a count/payload mismatch fails
authentication before or during decode.

## 2. Container

```
header 28 bytes
frames*
```

### Header (28 bytes)

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | magic, ASCII `RCMP` |
| 4 | 2 | version, u16, must be `1` |
| 6 | 2 | flags, u16 (bit 0: adaptive; other bits reserved) |
| 8 | 4 | bound, u32 (`1 ..= 2**24`) |
| 12 | 4 | alphabet, u32 |
| 16 | 8 | declared symbol count, u64 |
| 24 | 4 | CRC-32 (IEEE) over bytes 0..24 |

### Frames

Each frame is:

```
u8  marker
u32 body_len
u8  body[body_len]
u32 crc32(body)
```

Markers:

* `T` (0x54) — TABLE, begins a new epoch. Body:
  * u32 entries (must equal header alphabet)
  * u32 × entries: frequencies.
* `C` (0x43) — CHUNK. Body:
  * u64 start_symbol (must equal the running count of preceding chunks)
  * u32 epoch (must reference a preceding TABLE)
  * u32 symbol_count (must be ≥ 1)
  * u32 payload_len
  * u8  payload[payload_len] — one complete kernel stream.
* `E` (0x45) — EOF, last frame. Body:
  * u64 final symbol count (must equal the header declaration).

### Validation rules (each maps to one `ContainerError` variant)

* first frame must be TABLE epoch 0 (`MissingBaselineTable`);
* table epochs strictly increasing (`BadEpoch`); chunk references known
  (`UnknownEpoch`);
* chunks tile `[0, declared)` without gaps or overlap
  (`OutOfOrderChunks`, `SymbolCountMismatch`);
* every frame CRC and the header CRC verified
  (`FrameCrcMismatch`, `HeaderCrcMismatch`);
* EOF present, last, and consistent (`MissingEof`, `EofCountMismatch`,
  `TrailingBytesAfterEof`);
* all embedded frequency tables independently validated as in §1
  (`BadTable`);
* declared symbol count, alphabet and total bytes checked against the
  supplied `Budgets` before allocation (`BudgetExceeded`).

## 3. CRC-32

IEEE 802.3 polynomial (reflected `0xEDB88320`), init `0xFFFFFFFF`, final
XOR `0xFFFFFFFF` — identical to zlib/gzip/PNG. Check vector:
`crc32(b"123456789") == 0xCBF43926`.
