//! Wire encoding of the canonical code-length table and the decoder lookup.
//!
//! # Code-length table wire form
//!
//! The table precedes the packed body in every block payload:
//!
//! * `num_sym: u16` — number of symbols in the alphabet.
//! * `num_sym == 0` (empty input): nothing follows.
//! * `num_sym == 1` (lone-symbol alphabet): one `u8` symbol follows; its code
//!   length is the reserved zero and the body carries zero bits.
//! * `num_sym >= 2`: the *full 256-position length array* is run-length
//!   encoded from `first_sym` to `last_sym` inclusive:
//!     - `first_sym: u8`, `last_sym: u8` (`first_sym <= last`);
//!     - a DEFLATE-style opcode stream over the *full span* (an interior
//!       length byte of 0 means that symbol position is absent):
//!       - `0x01..=0xC8` (`1..=200`): that many literal length bytes follow,
//!         each in `0..=32` (0 = absent symbol position);
//!       - `0xC9..=0xFF`: copy the previous literal length
//!         `op - 0xC8` more times (1..=55 extra positions), including a
//!         previous zero. Opcode `0xC8` ("zero more copies") is reserved.
//!
//! Opcode `0x00` is also reserved and rejected. The number of non-zero
//! positions recovered from the stream must equal `num_sym`, otherwise the
//! table is malformed.
//!
//! No other opcodes exist; zero is unused and rejected. The emitted positions
//! must cover `first_sym..=last_sym` exactly, with no overruns.
//!
//! The decoder then re-derives canonical code words itself, so code words are
//! never stored. [`DecodeTable`] validates ordering, lengths and the Kraft
//! equality and provides bit-at-a-time canonical lookup.

use crate::error::{Error, ErrorKind, Result};
use crate::huffman::{
    canonical_codes, validate_complete, SymbolLength, ALPHABET_SIZE, MAX_CODE_BITS,
};

/// Maximum number of symbols the length-table RLE can express.
pub const MAX_SYMBOLS: u16 = 256;

/// Highest literal opcode: emit one literal length.
const OP_LITERAL_MAX: u8 = 0xC8;
/// First valid repeat opcode; extra copies encoded is `op - OP_REPEAT_BASE`
/// (1..=55). `OP_REPEAT_BASE` itself (0 copies) is reserved.
const OP_REPEAT_BASE: u8 = 0xC8;
/// Largest legal extra-copies count (`op = 0xFF`).
const MAX_REPEAT_RUN: usize = 55;

/// Append the code-length table for `table` (sorted `(symbol, length)`
/// entries as produced by [`crate::huffman::build_lengths`]) to `out`.
/// Mirrors the [`decode_table_header`] format documented above.
pub fn encode_table(table: &[SymbolLength], out: &mut Vec<u8>) -> Result<()> {
    let n = table.len();
    if n > MAX_SYMBOLS as usize {
        return Err(Error::new(
            ErrorKind::TooManySymbols,
            format!("{n} symbols exceeds {MAX_SYMBOLS}"),
        ));
    }
    out.extend_from_slice(&(n as u16).to_be_bytes());

    if n == 0 {
        return Ok(());
    }
    if n == 1 {
        // Reserved zero length; symbol carried explicitly.
        debug_assert_eq!(table[0].length, 0);
        out.push(table[0].symbol);
        return Ok(());
    }

    let mut lengths_by_symbol = [0u8; ALPHABET_SIZE];
    for sl in table {
        if sl.length == 0 {
            return Err(Error::new(
                ErrorKind::ZeroLengthInMultiSymbolTable,
                format!("symbol {} has zero length", sl.symbol),
            ));
        }
        lengths_by_symbol[sl.symbol as usize] = sl.length;
    }
    let present: Vec<u8> = (0..ALPHABET_SIZE)
        .filter(|&s| lengths_by_symbol[s] > 0)
        .map(|s| s as u8)
        .collect();
    debug_assert_eq!(present.len(), n);

    let first = present[0];
    let last = *present.last().unwrap();
    out.push(first);
    out.push(last);

    // Reduce the span to literal lengths and maximal same-length runs.
    let span: Vec<u8> = (first..=last)
        .map(|s| lengths_by_symbol[s as usize])
        .collect();

    // Greedy encoding.
    // * A same-length run of length >= 2: one literal byte followed by repeat
    //   opcodes (each covering 1..=55 extra copies).
    // * Otherwise consecutive "singleton" lengths are batched into one literal
    //   opcode carrying up to 200 bytes.
    let mut i = 0usize;
    while i < span.len() {
        let mut run = 1usize;
        while i + run < span.len() && span[i + run] == span[i] {
            run += 1;
        }
        if run >= 2 {
            // Literal occupies 1 position; repeats cover the remaining
            // `run - 1` extra copies.
            out.push(1u8);
            out.push(span[i]);
            let mut remaining = run - 1;
            while remaining > 0 {
                let take = remaining.min(MAX_REPEAT_RUN);
                out.push(OP_REPEAT_BASE + take as u8);
                remaining -= take;
            }
            i += run;
        } else {
            let start = i;
            while i < span.len() && (i - start) < OP_LITERAL_MAX as usize {
                let mut r = 1usize;
                while i + r < span.len() && span[i + r] == span[i] {
                    r += 1;
                }
                if r >= 2 {
                    break; // leave the run for the repeat branch
                }
                i += 1;
            }
            let count = i - start;
            debug_assert!(count >= 1 && count <= OP_LITERAL_MAX as usize);
            out.push(count as u8);
            out.extend_from_slice(&span[start..i]);
        }
    }
    Ok(())
}

/// Cursor over the table bytes, centralising bounds-checked reads.
struct Cursor<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Cursor { buf, pos: 0 }
    }

    fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    fn read_u8(&mut self, what: &'static str) -> Result<u8> {
        if self.pos >= self.buf.len() {
            return Err(Error::new(
                ErrorKind::TruncatedBlock,
                format!(
                    "code-length table truncated while reading {what} at byte {}",
                    self.pos
                ),
            ));
        }
        let v = self.buf[self.pos];
        self.pos += 1;
        Ok(v)
    }

    fn read_u16(&mut self, what: &'static str) -> Result<u16> {
        if self.remaining() < 2 {
            return Err(Error::new(
                ErrorKind::TruncatedBlock,
                format!("code-length table truncated while reading {what}"),
            ));
        }
        let v = u16::from_be_bytes([self.buf[self.pos], self.buf[self.pos + 1]]);
        self.pos += 2;
        Ok(v)
    }
}

/// Parse a code-length table from the front of `payload`, returning the
/// per-symbol length array (`0` = absent), the symbol count and the number of
/// payload bytes consumed.
///
/// `num_sym == 1` returns a single zero-length entry (lone-symbol form).
pub fn decode_table_header(payload: &[u8]) -> Result<(Vec<SymbolLength>, usize)> {
    let mut cur = Cursor::new(payload);
    let num_sym = cur.read_u16("num_sym")?;
    if num_sym > MAX_SYMBOLS {
        return Err(Error::new(
            ErrorKind::TooManySymbols,
            format!("num_sym {num_sym} exceeds alphabet {MAX_SYMBOLS}"),
        ));
    }

    if num_sym == 0 {
        return Ok((Vec::new(), cur.pos));
    }

    if num_sym == 1 {
        let sym = cur.read_u8("lone symbol")?;
        return Ok((
            vec![SymbolLength {
                symbol: sym,
                length: 0,
            }],
            cur.pos,
        ));
    }

    let first = cur.read_u8("first_sym")?;
    let last = cur.read_u8("last_sym")?;
    if first > last {
        return Err(Error::new(
            ErrorKind::DuplicateSymbol,
            format!("code-length table first_sym {first} > last_sym {last}"),
        ));
    }
    let span_len = (last as usize) - (first as usize) + 1;
    if span_len < num_sym as usize {
        return Err(Error::new(
            ErrorKind::DuplicateSymbol,
            format!("span {first}..={last} holds {span_len} positions but num_sym is {num_sym}"),
        ));
    }

    let mut lengths = [0u8; ALPHABET_SIZE];
    let mut emitted = 0usize;
    let mut prev_len: Option<u8> = None;

    while emitted < span_len {
        let op = cur.read_u8("rle opcode")?;
        if op == 0 {
            return Err(Error::new(
                ErrorKind::ReservedRleOpcode,
                format!("reserved opcode 0x00 at position {emitted}"),
            ));
        }
        if op <= OP_LITERAL_MAX {
            // `op` independent length bytes follow. A length byte is 0..=32:
            // zero means that symbol position inside the span is absent (this
            // is how an alphabet with interior gaps is expressed, e.g.
            // symbols {0,2} share a span 0..=2).
            let count = op as usize;
            for _ in 0..count {
                let lit = cur.read_u8("literal length")?;
                if lit > MAX_CODE_BITS {
                    return Err(Error::new(
                        ErrorKind::CodeLengthTooLong,
                        format!("literal length {lit} out of range 0..={MAX_CODE_BITS}"),
                    ));
                }
                if emitted >= span_len {
                    return Err(Error::new(
                        ErrorKind::InvalidRleRun,
                        "literal run overruns the declared symbol span",
                    ));
                }
                lengths[first as usize + emitted] = lit;
                emitted += 1;
                prev_len = Some(lit);
            }
        } else {
            let repeat = (op - OP_REPEAT_BASE) as usize; // 1..=55 extra
            if repeat == 0 {
                return Err(Error::new(
                    ErrorKind::ReservedRleOpcode,
                    "repeat opcode 0xC8 encodes zero extra copies and is reserved",
                ));
            }
            let Some(last_len) = prev_len else {
                return Err(Error::new(
                    ErrorKind::InvalidRleRun,
                    "repeat opcode before any literal length",
                ));
            };
            if last_len > MAX_CODE_BITS {
                return Err(Error::new(
                    ErrorKind::CodeLengthTooLong,
                    format!("repeated length {last_len} out of range 0..={MAX_CODE_BITS}"),
                ));
            };
            if emitted + repeat > span_len {
                return Err(Error::new(
                    ErrorKind::InvalidRleRun,
                    format!(
                        "repeat run of {repeat} overruns the {span_len}-position span at {emitted}"
                    ),
                ));
            }
            for _ in 0..repeat {
                lengths[first as usize + emitted] = last_len;
                emitted += 1;
            }
        }
    }

    // Exact coverage.
    if emitted != span_len {
        return Err(Error::new(
            ErrorKind::InvalidRleRun,
            format!("RLE covered {emitted} positions, span is {span_len}"),
        ));
    }

    // Build the sorted (length, symbol) table for present positions only.
    let mut table: Vec<SymbolLength> = Vec::with_capacity(num_sym as usize);
    let mut prev_symbol: Option<u8> = None;
    for sym in first..=last {
        let l = lengths[sym as usize];
        if l == 0 {
            continue;
        }
        if let Some(p) = prev_symbol {
            if sym <= p {
                return Err(Error::new(
                    ErrorKind::DuplicateSymbol,
                    format!("duplicate/unsorted symbol {sym} after {p}"),
                ));
            }
        }
        prev_symbol = Some(sym);
        table.push(SymbolLength {
            symbol: sym,
            length: l,
        });
    }
    if table.len() != num_sym as usize {
        return Err(Error::new(
            ErrorKind::DuplicateSymbol,
            format!(
                "table holds {} non-zero lengths, num_sym declares {num_sym}",
                table.len()
            ),
        ));
    }

    // Canonical order + complete-prefix-code validation (lengths, Kraft).
    table.sort_unstable_by(|a, b| a.length.cmp(&b.length).then(a.symbol.cmp(&b.symbol)));
    validate_complete(&table)?;

    Ok((table, cur.pos))
}

/// Canonical decode table. For each code length `l` (1..=32) the table stores
/// the *first* canonical code of that length (MSB-aligned) and the index into
/// the sorted symbol list where that length starts.
#[derive(Debug)]
pub struct DecodeTable {
    /// Sorted `(length, symbol)` entries.
    pub entries: Vec<SymbolLength>,
    /// First code of each length, MSB-aligned; `None` if no code has it.
    first_code: [Option<u32>; (MAX_CODE_BITS as usize) + 1],
    /// Starting index in `entries` of each length.
    start_index: [usize; (MAX_CODE_BITS as usize) + 1],
}

impl DecodeTable {
    /// Build a validated decode table. Entries must already be sorted by
    /// `(length, symbol)` (as produced by [`decode_table_header`]).
    pub fn from_sorted(entries: Vec<SymbolLength>) -> Result<Self> {
        if entries.len() < 2 {
            return Err(Error::new(
                ErrorKind::OverSubscribedTree,
                "DecodeTable requires at least two symbols",
            ));
        }
        validate_complete(&entries)?;
        let codes = canonical_codes(&entries)?;
        let mut first_code = [None; 33];
        let mut start_index = [0usize; 33];
        for (i, sl) in entries.iter().enumerate() {
            let l = sl.length as usize;
            if first_code[l].is_none() {
                first_code[l] = Some(codes[i]);
                start_index[l] = i;
            }
        }
        Ok(DecodeTable {
            entries,
            first_code,
            start_index,
        })
    }

    /// Decode one symbol.
    ///
    /// * `code`: code bits accumulated so far, MSB-aligned
    /// * `len`: number of accumulated bits
    ///
    /// Returns `Some(symbol)` when a code is matched at exactly `len` bits.
    pub fn lookup(&self, code: u32, len: u8) -> Option<u8> {
        if len == 0 || len > MAX_CODE_BITS {
            return None;
        }
        let first = self.first_code[len as usize]?;
        // Both are MSB-aligned; difference of top `len` bits indexes the block.
        let diff = top_bits(code, len).checked_sub(top_bits(first, len))?;
        let idx = self.start_index[len as usize] + diff as usize;
        let entry = self.entries.get(idx)?;
        if entry.length != len {
            return None;
        }
        Some(entry.symbol)
    }
}

/// Return the top `len` bits of an MSB-aligned code as a left-justified
/// integer in `0..2^len`.
fn top_bits(code_msb_aligned: u32, len: u8) -> u32 {
    if len >= 32 {
        code_msb_aligned
    } else {
        code_msb_aligned >> (32 - len)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_table_roundtrip() {
        let mut buf = Vec::new();
        encode_table(&[], &mut buf).unwrap();
        let (table, consumed) = decode_table_header(&buf).unwrap();
        assert!(table.is_empty());
        assert_eq!(consumed, buf.len());
    }

    #[test]
    fn lone_symbol_roundtrip() {
        let mut buf = Vec::new();
        encode_table(
            &[SymbolLength {
                symbol: b'Q',
                length: 0,
            }],
            &mut buf,
        )
        .unwrap();
        let (table, consumed) = decode_table_header(&buf).unwrap();
        assert_eq!(
            table,
            vec![SymbolLength {
                symbol: b'Q',
                length: 0
            }]
        );
        assert_eq!(consumed, buf.len());
    }

    #[test]
    fn four_symbol_rle_roundtrip() {
        let table = (0u8..=3)
            .map(|s| SymbolLength {
                symbol: s,
                length: 2,
            })
            .collect::<Vec<_>>();
        let mut buf = Vec::new();
        encode_table(&table, &mut buf).unwrap();
        let (decoded, consumed) = decode_table_header(&buf).unwrap();
        assert_eq!(consumed, buf.len());
        assert_eq!(decoded.len(), 4);
        assert!(decoded.iter().all(|sl| sl.length == 2));
    }

    #[test]
    fn span_with_interior_absent_symbol() {
        // Symbols 0 and 2 present, symbol 1 absent: lengths (1,1) form a
        // complete code.
        let table = vec![
            SymbolLength {
                symbol: 0,
                length: 1,
            },
            SymbolLength {
                symbol: 2,
                length: 1,
            },
        ];
        let mut buf = Vec::new();
        encode_table(&table, &mut buf).unwrap();
        let (decoded, consumed) = decode_table_header(&buf).unwrap();
        assert_eq!(consumed, buf.len());
        let syms: Vec<u8> = decoded.iter().map(|sl| sl.symbol).collect();
        assert_eq!(syms, vec![0, 2]);
    }

    #[test]
    fn lookup_decodes_all_canonical_codes() {
        // Use the real kernel on the classic (45,13,12,16,9,5) profile rather
        // than hand-asserted lengths.
        let mut freq = [0u64; ALPHABET_SIZE];
        for (s, c) in [(0u8, 45u64), (1, 13), (2, 12), (3, 16), (4, 9), (5, 5)] {
            freq[s as usize] = c;
        }
        let sorted = crate::huffman::build_lengths(&freq).unwrap();
        assert_eq!(
            sorted
                .iter()
                .map(|sl| (sl.symbol, sl.length))
                .collect::<Vec<_>>(),
            vec![(0, 1), (1, 3), (2, 3), (3, 3), (4, 4), (5, 4)]
        );
        let mut buf = Vec::new();
        encode_table(&sorted, &mut buf).unwrap();
        let (table, _) = decode_table_header(&buf).unwrap();
        let dt = DecodeTable::from_sorted(table).unwrap();
        let pairs = [
            (0x0000_0000u32, 1u8, 0u8), // 0
            (0x8000_0000, 3, 1),        // 100
            (0xA000_0000, 3, 2),        // 101
            (0xC000_0000, 3, 3),        // 110
            (0xE000_0000, 4, 4),        // 1110
            (0xF000_0000, 4, 5),        // 1111
        ];
        for (code, len, sym) in pairs {
            assert_eq!(dt.lookup(code, len), Some(sym), "code {code:#x}/{len}");
        }
        // A 3-bit code read at depth 4 must not resolve (would imply an
        // over-subscribed/ambiguous tree).
        assert_eq!(dt.lookup(0xC000_0000, 4), None);
        // Unused 2-bit space 01... never resolves at length 2.
        assert_eq!(dt.lookup(0x4000_0000, 2), None);
    }

    #[test]
    fn repeat_before_literal_rejected() {
        // num_sym=2, span 0..=1, then a bare repeat opcode (2 extra copies).
        let payload = [0u8, 2, 0, 1, 0xCA];
        assert_eq!(
            decode_table_header(&payload).unwrap_err().kind(),
            ErrorKind::InvalidRleRun
        );
    }

    #[test]
    fn reserved_zero_opcode_rejected() {
        // num_sym=2, span 0..=1, then opcode 0x00.
        let payload = [0u8, 2, 0, 1, 0x00];
        assert_eq!(
            decode_table_header(&payload).unwrap_err().kind(),
            ErrorKind::ReservedRleOpcode
        );
    }

    #[test]
    fn over_subscribed_table_rejected_at_decode() {
        // Three length-1 entries: Kraft 3/2 > 1. num_sym=3, span 0..=2:
        // literal op 1,len1 ; literal op1,len1 ; literal op1,len1.
        let payload = [0u8, 3, 0, 2, 1, 1, 1, 1, 1, 1];
        assert_eq!(
            decode_table_header(&payload).unwrap_err().kind(),
            ErrorKind::OverSubscribedTree
        );
    }

    #[test]
    fn truncated_header_rejected() {
        assert_eq!(
            decode_table_header(&[]).unwrap_err().kind(),
            ErrorKind::TruncatedBlock
        );
        // num_sym=1 but no symbol byte.
        assert_eq!(
            decode_table_header(&[0, 1]).unwrap_err().kind(),
            ErrorKind::TruncatedBlock
        );
    }

    #[test]
    fn run_overrun_rejected() {
        // num_sym=2, span 0..=1 (2 positions), literal len2 then repeat of 5
        // extra copies.
        let payload = [0u8, 2, 0, 1, 1, 2, 0xCD];
        assert_eq!(
            decode_table_header(&payload).unwrap_err().kind(),
            ErrorKind::InvalidRleRun
        );
    }
}
