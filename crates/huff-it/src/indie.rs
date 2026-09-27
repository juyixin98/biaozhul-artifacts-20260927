//! Independent container decoder used only by tests.
//!
//! Strategy (deliberately unlike `huff-core`):
//!
//! 1. Parse the envelope by offsets and verify each CRC independently.
//! 2. Build an explicit binary trie out of the declared code lengths; each
//!    payload bit walks one edge. A leaf emits a symbol, a missing edge is
//!    [`Error::InvalidCodeword`], running out of bits is [`Error::Truncated`].
//! 3. Verify exact output length, zero padding and every original-data CRC.

use std::fmt;

/// Error categories produced by the independent decoder. Names line up with
/// the production machine codes so tests can assert exact failure classes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    HeaderTruncated,
    BadMagic,
    UnknownVersion,
    UnknownFlags,
    HeaderCrcMismatch,
    DirectoryTruncated,
    DirectoryBoundsInvalid,
    DirectoryCrcMismatch,
    DirectoryNotContiguous,
    BlockCrcMismatch,
    TotalLengthMismatch,
    TrailingData,
    PayloadTruncated,
    BitstreamTruncated,
    CodeLenTooLong,
    BadSymbolCount,
    TableKraftOverflow,
    TableIncomplete,
    Truncated,
    InvalidCodeword,
    InvalidPadding,
    OutputLengthMismatch,
}

impl Error {
    /// Production machine-code spelling (FORMAT.md §9).
    pub fn code(&self) -> &'static str {
        match self {
            Error::HeaderTruncated => "HEADER_TRUNCATED",
            Error::BadMagic => "BAD_MAGIC",
            Error::UnknownVersion => "UNKNOWN_VERSION",
            Error::UnknownFlags => "UNKNOWN_FLAGS",
            Error::HeaderCrcMismatch => "HEADER_CRC_MISMATCH",
            Error::DirectoryTruncated => "DIRECTORY_TRUNCATED",
            Error::DirectoryBoundsInvalid => "DIRECTORY_BOUNDS_INVALID",
            Error::DirectoryCrcMismatch => "DIRECTORY_CRC_MISMATCH",
            Error::DirectoryNotContiguous => "DIRECTORY_NOT_CONTIGUOUS",
            Error::BlockCrcMismatch => "BLOCK_CRC_MISMATCH",
            Error::TotalLengthMismatch => "TOTAL_LENGTH_MISMATCH",
            Error::TrailingData => "TRAILING_DATA",
            Error::PayloadTruncated => "BLOCK_PAYLOAD_TRUNCATED",
            Error::BitstreamTruncated => "BITSTREAM_TRUNCATED",
            Error::CodeLenTooLong => "CODE_LEN_TOO_LONG",
            Error::BadSymbolCount => "BAD_SYMBOL_COUNT",
            Error::TableKraftOverflow => "TABLE_KRAFT_OVERFLOW",
            Error::TableIncomplete => "TABLE_INCOMPLETE",
            Error::Truncated => "TRUNCATED_CODEWORD",
            Error::InvalidCodeword => "INVALID_CODEWORD",
            Error::InvalidPadding => "INVALID_PADDING",
            Error::OutputLengthMismatch => "OUTPUT_LENGTH_MISMATCH",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.code())
    }
}
impl std::error::Error for Error {}

const HEADER_LEN: usize = 32;
const FOOTER_LEN: usize = 16;
const DIR_ENTRY_LEN: usize = 16;
const FRAME_OVERHEAD: usize = 8;
const PAYLOAD_HEADER_LEN: usize = 12;
const BODY_OFFSET: usize = PAYLOAD_HEADER_LEN + 256;
const MAX_CODE_LEN: u8 = 32;

fn u32le(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

/// Independent CRC-32 (polynomial via table built at first use).
fn crc32(data: &[u8]) -> u32 {
    fn table() -> [u32; 256] {
        let mut t = [0u32; 256];
        let mut i = 0u32;
        while i < 256 {
            let mut c = i;
            let mut j = 0;
            while j < 8 {
                c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
                j += 1;
            }
            t[i as usize] = c;
            i += 1;
        }
        t
    }
    let t = table();
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc = t[((crc ^ b as u32) & 0xFF) as usize] ^ (crc >> 8);
    }
    crc ^ 0xFFFF_FFFF
}

/// Binary trie node for one code table.
struct Trie {
    children: Vec<[i32; 2]>,
    /// `Some(symbol)` at leaf nodes.
    symbol: Vec<Option<u8>>,
}

impl Trie {
    fn new() -> Self {
        Self { children: vec![[-1; 2]], symbol: vec![None] }
    }

    fn insert(&mut self, symbol: u8, code: u32, len: u8) -> Result<(), Error> {
        let mut node = 0usize;
        for depth in 1..=len {
            let bit = ((code >> (len - depth)) & 1) as usize;
            let next = self.children[node][bit];
            if next == -1 {
                let id = self.children.len() as i32;
                self.children.push([-1; 2]);
                self.symbol.push(None);
                self.children[node][bit] = id;
                node = id as usize;
            } else {
                node = next as usize;
                if self.symbol[node].is_some() {
                    // Code is a prefix of an earlier code: oversubscribed.
                    return Err(Error::TableKraftOverflow);
                }
            }
        }
        if self.children[node] != [-1, -1] {
            // Earlier code was a prefix of this one: oversubscribed.
            return Err(Error::TableKraftOverflow);
        }
        if self.symbol[node].is_some() {
            return Err(Error::TableKraftOverflow); // duplicate
        }
        self.symbol[node] = Some(symbol);
        Ok(())
    }
}

struct BlockMeta {
    orig_len: u32,
    payload_len: u32,
    frame_off: u32,
    orig_crc: u32,
}

/// Fully verify and decode a container, independently of `huff-core`.
pub fn decode(blob: &[u8]) -> Result<Vec<u8>, Error> {
    if blob.len() < HEADER_LEN {
        return Err(Error::HeaderTruncated);
    }
    if &blob[0..4] != b"HUFF" {
        return Err(Error::BadMagic);
    }
    if blob[4] != 1 {
        return Err(Error::UnknownVersion);
    }
    if blob[5] != 0 {
        return Err(Error::UnknownFlags);
    }
    if crc32(&blob[..28]) != u32le(&blob[28..32]) {
        return Err(Error::HeaderCrcMismatch);
    }

    let total = u32le(&blob[10..14]);
    let block_count = u32le(&blob[14..18]);
    let dir_offset = u32le(&blob[18..22]) as usize;
    let dir_len = u32le(&blob[22..26]) as usize;
    let footer = dir_offset + dir_len;
    if blob.len() < footer + FOOTER_LEN {
        return Err(Error::DirectoryTruncated);
    }
    if blob.len() != footer + FOOTER_LEN {
        return Err(Error::TrailingData);
    }
    let dir_crc = u32le(&blob[footer..footer + 4]);
    let f_off = u32le(&blob[footer + 4..footer + 8]) as usize;
    let f_len = u32le(&blob[footer + 8..footer + 12]) as usize;
    let f_count = u32le(&blob[footer + 12..footer + 16]);
    if f_off != dir_offset || f_len != dir_len || f_count != block_count {
        return Err(Error::DirectoryBoundsInvalid);
    }
    if crc32(&blob[dir_offset..footer]) != dir_crc {
        return Err(Error::DirectoryCrcMismatch);
    }
    if block_count == 0 || dir_len != block_count as usize * DIR_ENTRY_LEN {
        return Err(Error::DirectoryBoundsInvalid);
    }

    let mut blocks = Vec::with_capacity(block_count as usize);
    let mut expected_off = HEADER_LEN as u32;
    for i in 0..block_count as usize {
        let e = dir_offset + i * DIR_ENTRY_LEN;
        let b = BlockMeta {
            orig_len: u32le(&blob[e..e + 4]),
            payload_len: u32le(&blob[e + 4..e + 8]),
            frame_off: u32le(&blob[e + 8..e + 12]),
            orig_crc: u32le(&blob[e + 12..e + 16]),
        };
        if b.frame_off != expected_off {
            return Err(Error::DirectoryNotContiguous);
        }
        let start = b.frame_off as usize;
        let frame_len = FRAME_OVERHEAD as u32 + b.payload_len;
        let end = start.checked_add(frame_len as usize).ok_or(Error::DirectoryBoundsInvalid)?;
        if end > dir_offset || end > blob.len() {
            return Err(Error::DirectoryBoundsInvalid);
        }
        if u32le(&blob[start..start + 4]) != b.payload_len {
            return Err(Error::DirectoryBoundsInvalid);
        }
        let payload = &blob[start + FRAME_OVERHEAD..end];
        if crc32(payload) != u32le(&blob[start + 4..start + 8]) {
            return Err(Error::BlockCrcMismatch);
        }
        expected_off = end as u32;
        blocks.push(b);
    }
    if expected_off as usize != dir_offset {
        return Err(Error::DirectoryNotContiguous);
    }

    let mut out: Vec<u8> = Vec::new();
    for b in &blocks {
        let start = b.frame_off as usize + FRAME_OVERHEAD;
        let payload = &blob[start..start + b.payload_len as usize];
        if payload.len() < BODY_OFFSET {
            return Err(Error::PayloadTruncated);
        }
        let original_len = u32le(&payload[0..4]);
        let bits_total = u32le(&payload[4..8]);
        let symbol_count = u16::from_le_bytes([payload[8], payload[9]]);
        let sole_symbol = payload[10];
        if payload[11] != 0 {
            return Err(Error::UnknownFlags);
        }
        let lengths = &payload[PAYLOAD_HEADER_LEN..BODY_OFFSET];
        let body = &payload[BODY_OFFSET..];

        if (bits_total as u64) > body.len() as u64 * 8 {
            return Err(Error::BitstreamTruncated);
        }

        if original_len == 0 {
            if symbol_count != 0 || bits_total != 0 || !body.is_empty() {
                return Err(Error::OutputLengthMismatch);
            }
            continue;
        }

        if symbol_count == 0 {
            return Err(Error::BadSymbolCount);
        }
        if symbol_count == 1 {
            if lengths.iter().any(|&l| l != 0) {
                return Err(Error::TableIncomplete);
            }
            if bits_total != 0 {
                return Err(Error::InvalidPadding);
            }
            out.extend(std::iter::repeat(sole_symbol).take(original_len as usize));
        } else {
            // Validate lengths, Kraft inequality and build the trie directly.
            let mut kraft: u128 = 0;
            let mut positive = 0u16;
            for &l in lengths {
                if l > MAX_CODE_LEN {
                    return Err(Error::CodeLenTooLong);
                }
                if l > 0 {
                    positive += 1;
                    kraft += 1u128 << (MAX_CODE_LEN - l);
                }
            }
            if positive != symbol_count {
                return Err(Error::BadSymbolCount);
            }
            if kraft > (1u128 << MAX_CODE_LEN) {
                return Err(Error::TableKraftOverflow);
            }

            // Canonical code assignment (same recurrence as the format, but
            // the values are consumed here through trie walks).
            let mut trie = Trie::new();
            let mut running = 0u32;
            for len in 1u8..=MAX_CODE_LEN {
                let count = lengths.iter().filter(|&&l| l == len).count() as u32;
                running <<= 1;
                if count > 0 {
                    let base = running;
                    let mut at: Vec<u8> = (0u16..256)
                        .filter(|&s| lengths[s as usize] == len)
                        .map(|s| s as u8)
                        .collect();
                    at.sort_unstable();
                    for (i, symbol) in at.iter().enumerate() {
                        trie.insert(*symbol, base + i as u32, len)?;
                    }
                    running += count;
                }
            }

            let mut node = 0usize;
            let mut depth = 0u8;
            let mut emitted = 0u32;
            for bit_index in 0..bits_total {
                let bit = ((body[(bit_index >> 3) as usize] >> (7 - (bit_index & 7))) & 1) as usize;
                let next = trie.children[node][bit];
                if next == -1 {
                    return Err(Error::InvalidCodeword);
                }
                node = next as usize;
                depth += 1;
                if let Some(symbol) = trie.symbol[node] {
                    if emitted >= original_len {
                        return Err(Error::OutputLengthMismatch); // no unbounded output
                    }
                    out.push(symbol);
                    emitted += 1;
                    node = 0;
                    depth = 0;
                }
            }
            if node != 0 || depth != 0 {
                return Err(Error::Truncated);
            }
            if emitted != original_len {
                return Err(Error::OutputLengthMismatch);
            }
            for p in bits_total as u64..(body.len() as u64 * 8) {
                let v = (body[(p >> 3) as usize] >> (7 - (p & 7))) & 1;
                if v != 0 {
                    return Err(Error::InvalidPadding);
                }
            }
        }

        if original_len != b.orig_len {
            return Err(Error::TotalLengthMismatch);
        }
        let decoded_start = out.len() - original_len as usize;
        if crc32(&out[decoded_start..]) != b.orig_crc {
            return Err(Error::BlockCrcMismatch);
        }
    }

    if out.len() as u32 != total {
        return Err(Error::TotalLengthMismatch);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_input() {
        // Minimal independent check of the empty convention shape.
        let mut blob = Vec::new();
        blob.extend_from_slice(b"HUFF");
        blob.push(1); // version
        blob.push(0); // flags
        blob.extend_from_slice(&65536u32.to_le_bytes());
        blob.extend_from_slice(&0u32.to_le_bytes()); // total
        blob.extend_from_slice(&1u32.to_le_bytes()); // blocks
        let frame_len = (FRAME_OVERHEAD + BODY_OFFSET) as u32;
        let dir_offset = HEADER_LEN as u32 + frame_len;
        blob.extend_from_slice(&dir_offset.to_le_bytes());
        blob.extend_from_slice(&(DIR_ENTRY_LEN as u32).to_le_bytes());
        blob.extend_from_slice(&0u16.to_le_bytes());
        blob.extend_from_slice(&crc32(&blob).to_le_bytes());
        // frame
        blob.extend_from_slice(&(BODY_OFFSET as u32).to_le_bytes());
        let mut payload = vec![0u8; BODY_OFFSET];
        blob.extend_from_slice(&crc32(&payload).to_le_bytes());
        blob.append(&mut payload);
        // directory
        let mut dir = Vec::new();
        dir.extend_from_slice(&0u32.to_le_bytes());
        dir.extend_from_slice(&(BODY_OFFSET as u32).to_le_bytes());
        dir.extend_from_slice(&(HEADER_LEN as u32).to_le_bytes());
        dir.extend_from_slice(&crc32(b"").to_le_bytes());
        let dir_crc = crc32(&dir);
        blob.extend_from_slice(&dir);
        blob.extend_from_slice(&dir_crc.to_le_bytes());
        blob.extend_from_slice(&dir_offset.to_le_bytes());
        blob.extend_from_slice(&(DIR_ENTRY_LEN as u32).to_le_bytes());
        blob.extend_from_slice(&1u32.to_le_bytes());

        assert!(decode(&blob).unwrap().is_empty());
    }

    #[test]
    fn unknown_version() {
        let blob = vec![0u8; HEADER_LEN]; // too short fields but magic checked later
        let mut blob = blob;
        blob[0..4].copy_from_slice(b"HUFF");
        blob[4] = 5;
        assert_eq!(decode(&blob).unwrap_err(), Error::UnknownVersion);
    }
}
