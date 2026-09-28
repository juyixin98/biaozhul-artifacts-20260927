//! Independent from-scratch decoder used ONLY by integration tests.
//!
//! This deliberately does not call any of the crate's decode functions:
//! it re-parses the HCMP container, code-length table and packed bitstream
//! with its own code (different data structures — flat maps keyed on
//! `(length, code)` rather than the crate's `DecodeTable`). Its job is to
//! catch divergence between the real codec, the Python oracle and this
//! third implementation.
//!
//! Errors are returned as the same stable category strings used by
//! [`hcomp::error::ErrorKind::as_str`], so golden tests can compare them
//! directly.

#![allow(clippy::needless_range_loop)]

const MAGIC: &[u8; 4] = b"HCMP";
const VERSION: u8 = 1;
const GLOBAL_HEADER_LEN: usize = 16;
const DIR_ENTRY_LEN: usize = 32;
const MAX_CODE_BITS: u8 = 32;

const OP_LITERAL_MAX: u8 = 0xC8;
const OP_REPEAT_BASE: u8 = 0xC8;

/// Independent CRC-32 (IEEE).
pub mod crc {
    const POLY: u32 = 0xEDB8_8320;

    fn table() -> [u32; 256] {
        let mut t = [0u32; 256];
        let mut n = 0usize;
        while n < 256 {
            let mut c = n as u32;
            let mut k = 0;
            while k < 8 {
                c = if c & 1 != 0 { POLY ^ (c >> 1) } else { c >> 1 };
                k += 1;
            }
            t[n] = c;
            n += 1;
        }
        t
    }

    pub fn checksum(bytes: &[u8]) -> u32 {
        let t = table();
        let mut c = u32::MAX;
        for b in bytes {
            c = t[((c ^ *b as u32) & 0xFF) as usize] ^ (c >> 8);
        }
        c ^ u32::MAX
    }
}

/// Decode a whole container the independent way. Returns decoded block
/// bytes in directory order, or a stable error-category string.
pub fn decode_container(blob: &[u8]) -> Result<Vec<Vec<u8>>, String> {
    if blob.len() < GLOBAL_HEADER_LEN {
        return Err("truncated_header".to_string());
    }
    if &blob[0..4] != MAGIC {
        return Err("bad_magic".to_string());
    }
    let version = blob[4];
    if version != VERSION {
        return Err("unknown_version".to_string());
    }
    if blob[5] != 0 || blob[6..8] != [0, 0] {
        return Err("bad_flags".to_string());
    }
    let count = u32::from_be_bytes([blob[8], blob[9], blob[10], blob[11]]) as usize;
    let dir_crc_stored = u32::from_be_bytes([blob[12], blob[13], blob[14], blob[15]]);

    let dir_end = GLOBAL_HEADER_LEN
        .checked_add(DIR_ENTRY_LEN.checked_mul(count).ok_or("too_many_blocks")?)
        .ok_or("truncated_directory")?;
    if dir_end > blob.len() {
        return Err("truncated_directory".to_string());
    }
    let directory = &blob[GLOBAL_HEADER_LEN..dir_end];
    if crc::checksum(directory) != dir_crc_stored {
        return Err("directory_crc_mismatch".to_string());
    }

    struct Ent {
        original_len: u64,
        off: usize,
        len: usize,
        payload_crc: u32,
        original_crc: u32,
    }

    let mut entries = Vec::with_capacity(count);
    for i in 0..count {
        let r = &directory[i * DIR_ENTRY_LEN..(i + 1) * DIR_ENTRY_LEN];
        let original_len = u64::from_be_bytes(r[4..12].try_into().unwrap());
        let off = u64::from_be_bytes(r[12..20].try_into().unwrap()) as usize;
        let len = u32::from_be_bytes(r[20..24].try_into().unwrap()) as usize;
        let payload_crc = u32::from_be_bytes(r[24..28].try_into().unwrap());
        let original_crc = u32::from_be_bytes(r[28..32].try_into().unwrap());
        entries.push(Ent {
            original_len,
            off,
            len,
            payload_crc,
            original_crc,
        });
    }

    // Contiguity and payload-at-rest CRC.
    let mut expected = dir_end;
    for e in &entries {
        if e.off != expected {
            return Err("payload_overlap".to_string());
        }
        let end = e.off.checked_add(e.len).ok_or("payload_out_of_bounds")?;
        if end > blob.len() {
            return Err("payload_out_of_bounds".to_string());
        }
        expected = end;
    }
    if expected != blob.len() {
        return Err("trailing_garbage".to_string());
    }

    let mut out = Vec::with_capacity(count);
    for e in &entries {
        let payload = &blob[e.off..e.off + e.len];
        if crc::checksum(payload) != e.payload_crc {
            return Err("payload_crc_mismatch".to_string());
        }
        let decoded = decode_block(payload, e.original_len)?;
        if decoded.len() as u64 != e.original_len {
            return Err("length_mismatch".to_string());
        }
        if crc::checksum(&decoded) != e.original_crc {
            return Err("original_crc_mismatch".to_string());
        }
        out.push(decoded);
    }
    Ok(out)
}

fn be_u16(p: &[u8]) -> u16 {
    u16::from_be_bytes([p[0], p[1]])
}
fn be_u64(p: &[u8]) -> u64 {
    u64::from_be_bytes(p[0..8].try_into().unwrap())
}

fn decode_block(payload: &[u8], expected_len: u64) -> Result<Vec<u8>, String> {
    if payload.len() < 2 {
        return Err("truncated_block".to_string());
    }
    let num = be_u16(&payload[0..2]) as usize;
    let mut pos = 2usize;

    if num == 0 {
        if payload.len() < pos + 8 {
            return Err("truncated_block".to_string());
        }
        let bits = be_u64(&payload[pos..pos + 8]);
        if bits != 0 || expected_len != 0 {
            return Err("length_mismatch".to_string());
        }
        if payload.len() != pos + 8 {
            return Err("truncated_bitstream".to_string());
        }
        return Ok(Vec::new());
    }

    if num == 1 {
        if payload.len() < pos + 1 + 8 {
            return Err("truncated_block".to_string());
        }
        let sym = payload[pos];
        pos += 1;
        let bits = be_u64(&payload[pos..pos + 8]);
        if bits != 0 {
            return Err("invalid_single_symbol_code".to_string());
        }
        if payload.len() != pos + 8 {
            return Err("truncated_bitstream".to_string());
        }
        return Ok(vec![sym; expected_len as usize]);
    }

    if payload.len() < pos + 2 {
        return Err("truncated_block".to_string());
    }
    let first = payload[pos] as usize;
    let last = payload[pos + 1] as usize;
    pos += 2;
    if first > last {
        return Err("duplicate_symbol".to_string());
    }
    let span_len = last - first + 1;

    let mut lengths = [0u8; 256];
    let mut emitted = 0usize;
    let mut prev: Option<u8> = None;
    while emitted < span_len {
        if pos >= payload.len() {
            return Err("truncated_block".to_string());
        }
        let op = payload[pos];
        pos += 1;
        if op == 0 {
            return Err("reserved_rle_opcode".to_string());
        }
        if op <= OP_LITERAL_MAX {
            for _ in 0..op as usize {
                if pos >= payload.len() {
                    return Err("truncated_block".to_string());
                }
                let lit = payload[pos];
                pos += 1;
                if lit > MAX_CODE_BITS {
                    return Err("code_length_too_long".to_string());
                }
                if emitted >= span_len {
                    return Err("invalid_rle_run".to_string());
                }
                lengths[first + emitted] = lit;
                emitted += 1;
                prev = Some(lit);
            }
        } else {
            let repeat = (op - OP_REPEAT_BASE) as usize;
            if repeat == 0 {
                return Err("reserved_rle_opcode".to_string());
            }
            let last_len = prev.ok_or("invalid_rle_run")?;
            if emitted + repeat > span_len {
                return Err("invalid_rle_run".to_string());
            }
            for _ in 0..repeat {
                lengths[first + emitted] = last_len;
                emitted += 1;
            }
        }
    }
    if emitted != span_len {
        return Err("invalid_rle_run".to_string());
    }

    // Collect present symbols, verify count and build canonical code map.
    let mut table: Vec<(u8, u8)> = Vec::new();
    for s in first..=last {
        if lengths[s] > 0 {
            table.push((s as u8, lengths[s]));
        }
    }
    if table.len() != num {
        return Err("duplicate_symbol".to_string());
    }
    table.sort_unstable_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)));
    for (_, l) in &table {
        if *l == 0 || *l > MAX_CODE_BITS {
            return Err("zero_length_in_multi_symbol_table".to_string());
        }
    }
    let maxl = *table.iter().map(|(_, l)| l).max().unwrap() as u32;
    let kraft: u128 = table.iter().map(|(_, l)| 1u128 << (maxl - *l as u32)).sum();
    if kraft != 1u128 << maxl {
        return Err("over_subscribed_tree".to_string());
    }

    if payload.len() < pos + 8 {
        return Err("truncated_block".to_string());
    }
    let bit_total = be_u64(&payload[pos..pos + 8]) as usize;
    pos += 8;
    let body = &payload[pos..];
    let body_needed = bit_total.div_ceil(8);
    if body.len() != body_needed {
        return Err("truncated_bitstream".to_string());
    }

    // Flat (length, integer-code) -> symbol map via the canonical recurrence.
    let mut code_map = std::collections::HashMap::<(u8, u32), u8>::new();
    let mut code: u32 = 0;
    let mut prev_len = table[0].1;
    for (i, (sym, len)) in table.iter().enumerate() {
        if i > 0 {
            code = (code + 1) << (*len - prev_len);
        }
        code_map.insert((*len, code), *sym);
        prev_len = *len;
    }

    let mut out: Vec<u8> = Vec::new();
    let mut acc: u32 = 0;
    let mut depth: u8 = 0;
    for bit_index in 0..bit_total {
        let byte = body[bit_index / 8];
        let bit = (byte >> (7 - (bit_index % 8))) & 1;
        acc = (acc << 1) | bit as u32;
        depth += 1;
        if depth > MAX_CODE_BITS {
            return Err("unknown_code".to_string());
        }
        if let Some(sym) = code_map.get(&(depth, acc)).copied() {
            out.push(sym);
            acc = 0;
            depth = 0;
            if out.len() as u64 > expected_len {
                return Err("length_mismatch".to_string());
            }
        }
    }
    if depth != 0 {
        return Err("truncated_bitstream".to_string());
    }
    if out.len() as u64 != expected_len {
        return Err("length_mismatch".to_string());
    }
    // Verify every trailing padding bit is zero.
    for bit_index in bit_total..body.len() * 8 {
        let byte = body[bit_index / 8];
        if (byte >> (7 - (bit_index % 8))) & 1 != 0 {
            return Err("invalid_trailing_bits".to_string());
        }
    }
    Ok(out)
}
