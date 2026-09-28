# HCMP container format specification (version 1)

HCMP stores one or more independently **canonical-Huffman** coded blocks in a
single file, preceded by a CRC-protected **block directory**. All integers are
big-endian. CRC-32 is the IEEE/reflected polynomial `0xEDB8_8320` with
initial/final XOR `0xFFFF_FFFF` (the same polynomial zlib uses).

## 1. Global header (16 bytes, fixed)

| offset | size | field          | value / meaning                                  |
|-------:|-----:|----------------|--------------------------------------------------|
| 0      | 4    | magic          | `48 43 4D 50` = ASCII `HCMP`                     |
| 4      | 1    | version        | `1`. Any other value is rejected as `unknown_version`. |
| 5      | 1    | flags          | `0`. Any set bit is rejected as `bad_flags`.     |
| 6      | 2    | reserved       | `0x00 0x00`; non-zero is rejected.               |
| 8      | 4    | block_count    | number of directory entries / blocks (`u32`).    |
| 12     | 4    | directory_crc32| CRC-32 over the directory bytes that follow.     |

A file shorter than 16 bytes is rejected as `truncated_header`. The magic,
version and flags are checked **before** any other byte is trusted.

## 2. Block directory

Immediately after the global header come `block_count` fixed 32-byte entries,
followed immediately by the packed payload region. The CRC in the global
header covers exactly these directory bytes.

Each entry:

| offset | size | field          | meaning                                           |
|-------:|-----:|----------------|---------------------------------------------------|
| 0      | 4    | block_id       | `u32`; must be unique (`duplicate_block_id`).     |
| 4      | 8    | original_len   | `u64`; decoded byte count.                         |
| 12     | 8    | payload_off    | `u64`; absolute file offset of this block payload.|
| 20     | 4    | payload_len    | `u32`; payload byte count.                         |
| 24     | 4    | payload_crc32  | CRC-32 of the payload bytes **at rest**.           |
| 28     | 4    | original_crc32 | CRC-32 of the decoded original bytes.             |

Structural invariants enforced on decode, in order:

1. `directory_crc32` matches;
2. block ids are unique;
3. `payload_off` values are strictly contiguous starting at
   `16 + 32*block_count` (`payload_overlap` otherwise);
4. every `[payload_off, payload_off+payload_len)` lies inside the file
   (`payload_out_of_bounds`);
5. each `payload_crc32` matches the bytes at rest;
6. the last payload ends exactly at EOF — trailing bytes are
   `trailing_garbage`.

## 3. Block payload

```
code-length table     (variable; see §4)
bit_total : u64       number of content bits in the packed body
body                  ceil(bit_total / 8) bytes, MSB-first, zero padded
```

`body` length must equal `ceil(bit_total/8)` exactly. Content bits are
transmitted high-bit-first in each byte. The `bit_total % 8` (or, when
`bit_total` is a multiple of 8, zero) remaining low bits of the final byte are
padding and **must all be zero** (`invalid_trailing_bits` otherwise).

### 3.1 Empty input

The alphabet table is `num_sym = 0`, the body has `bit_total = 0` and zero
bytes. `original_len` must be zero. This is an independently-defined encoding
of the empty byte string.

### 3.2 Lone-symbol input

If the input uses exactly one distinct byte, `num_sym = 1` and the one symbol
byte follows. Its code length is the reserved value **zero**: no code word is
assigned and the body has `bit_total = 0` and zero bytes. The decoder expands
to exactly `original_len` copies of that symbol. A lone-symbol table with a
non-zero `bit_total` is rejected (`invalid_single_symbol_code`).

### 3.3 Multi-symbol input

Each byte is encoded with its canonical code word. On decode the number of
symbols emitted must equal `original_len` (`length_mismatch`); reading may not
cross the content-bit budget (`truncated_bitstream`), a half-read code at the
end is `truncated_bitstream`, and a prefix longer than 32 bits that matches no
code is `unknown_code`. The decoder allocates at most `original_len` bytes and
aborts if a stream would emit more, so crafted blocks cannot cause unbounded
output. After decode, the original CRC is verified (`original_crc_mismatch`).

## 4. Code-length table (canonical, RLE)

Only `(symbol, length)` pairs are stored — code words are re-derived.

```
num_sym : u16
```

* `num_sym = 0` — nothing else (empty input).
* `num_sym = 1` — one symbol byte follows (lone-symbol form).
* `num_sym = 2..=256` — then:
  ```
  first_sym : u8
  last_sym  : u8       (first_sym <= last_sym)
  opcode stream covering the full span first_sym..=last_sym
  ```
  The span may contain absent positions (interior length byte zero).

Opcode stream:

* `0x01..=0xC8` (1..=200) — that many literal length bytes follow, each in
  `0..=32` (0 = that symbol position is absent).
* `0xC9..=0xFF` — copy the preceding literal length `op - 0xC8` more times
  (1..=55 extra positions), including a preceding zero.
* `0x00` is reserved (`reserved_rle_opcode`); `0xC8` ("zero more copies") is
  also reserved.

After expansion the number of non-zero positions must equal `num_sym`
(otherwise `duplicate_symbol`), every positive length must be in `1..=32`
(`code_length_too_long`), and the length set must satisfy the Kraft **equality**

```
sum over symbols of 2^-length == 1
```

(scaled by `2^max_length`). Both an over-subscribed set (`sum > 1`, code words
would overlap) and an incomplete set (`sum < 1`, unused codes that hide
ambiguity) are rejected as `over_subscribed_tree`.

## 5. Huffman construction and the tie-break rule

Lengths come from the Huffman algorithm using a min-priority queue keyed on,
in order:

1. **weight** — lower total frequency first;
2. **tree height** — when weights tie, the *shallower* tree is popped first;
3. **minimum leaf symbol** — final deterministic discriminator.

All three components are fixed by the input frequencies and symbol values, so
the code-length table is a pure deterministic function of the input. The
height component is what keeps weight-tied merges balanced: for frequencies
`(1,1,1,2)` on symbols `0..=3` every symbol receives length 2 (rather than the
`(3,3,2,1)` a min-symbol-only merge would produce).

Canonical code words are then derived by sorting `(symbol,length)` by
`(length asc, symbol asc)` and applying

```
code[0] = 0
code[i] = (code[i-1] + 1) << (length[i] - length[i-1])
```

emitting bits MSB-first.

## 6. Error categories

Stable machine-readable categories (also used as the JSON `error` field and in
test assertions):

`block_too_large`, `code_length_too_long`, `over_subscribed_tree`,
`invalid_rle_run`, `reserved_rle_opcode`, `duplicate_symbol`,
`zero_length_in_multi_symbol_table`, `invalid_single_symbol_code`,
`too_many_symbols`, `truncated_block`, `truncated_bitstream`,
`invalid_trailing_bits`, `unknown_code`, `length_mismatch`,
`payload_crc_mismatch`, `original_crc_mismatch`, `bad_magic`,
`unknown_version`, `bad_flags`, `truncated_header`, `truncated_directory`,
`directory_crc_mismatch`, `too_many_blocks`, `duplicate_block_id`,
`payload_overlap`, `payload_out_of_bounds`, `trailing_garbage`,
`not_found`, `invalid_id`, `already_exists`, `io_error`.

No malformed or unknown-state input is ever reported as success.
