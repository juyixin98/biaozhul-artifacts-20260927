//! Chunked container: a header, alternating TABLE/CHUNK frames, and an EOF.
//!
//! ```text
//! header  (32 bytes, big-endian; see HEADER_* docs)
//! frame*  TABLE epoch  (T, u32 len, freqs[len], u32 crc)
//!         CHUNK epoch  (C, u64 symbols, u32 epoch, u32 len, range payload, u32 crc)
//! frame   EOF          (E, u64 final symbol count, u32 crc)
//! ```
//!
//! CRC in every frame protects the bytes before it.  A CHUNK's range payload
//! is independently a complete kernel stream (seed + symbols + terminator).
//! Decoding needs only the TABLE epoch referenced by each CHUNK, so chunks
//! are independently addressable.

use crate::error::{ContainerError, DecodeError, TableError};
use crate::format::{
    crc32, BeReader, BeWrite, FLAG_ADAPTIVE, FORMAT_VERSION, HEADER_LEN, KNOWN_FLAGS, MAGIC,
    SEG_CHUNK, SEG_EOF, SEG_TABLE,
};
use crate::range::{decode_vec, RangeEncoder};
use crate::rescale::{AdaptiveModel, ObserveOutcome};
use crate::table::{FreqTable, MAX_BOUND};

/// Largest table (number of symbols) and frame size we will accept.
pub const MAX_ALPHABET: u32 = MAX_BOUND;
const MAX_FRAME_LEN: u64 = 1 << 30; // 1 GiB defensive ceiling

/// Decoding budgets; zero means "unbounded" for that dimension.
#[derive(Debug, Clone)]
pub struct Budgets {
    /// Maximum declared symbol count.
    pub max_symbols: u64,
    /// Maximum container byte length.
    pub max_bytes: u64,
    /// Maximum alphabet entries accepted in a TABLE frame.
    pub max_alphabet: u32,
}

impl Default for Budgets {
    fn default() -> Self {
        Self {
            max_symbols: 1 << 28,
            max_bytes: 1 << 30,
            max_alphabet: 1 << 20,
        }
    }
}

/// One coded chunk after parsing/decoding.
#[derive(Debug, Clone)]
pub struct Chunk {
    pub epoch: u32,
    pub symbols: Vec<u32>,
    /// Byte span of the whole frame in the container (diagnostics).
    pub frame_start: usize,
    pub frame_end: usize,
}

/// Result of parsing and fully decoding a container.
#[derive(Debug, Clone)]
pub struct ParsedContainer {
    pub flags: u16,
    pub bound: u32,
    pub alphabet: u32,
    pub declared_symbols: u64,
    pub tables: Vec<FreqTable>,
    pub chunks: Vec<Chunk>,
    /// All decoded symbols, concatenated in chunk order.
    pub symbols: Vec<u32>,
}

// ---------------------------------------------------------------------------
// Writing
// ---------------------------------------------------------------------------

/// Encode a byte message with one static table.
///
/// `alphabet` defaults to 256 when `None`; symbols are the raw byte values.
pub fn encode_static(
    data: &[u8],
    table: &FreqTable,
    alphabet: u32,
) -> Result<Vec<u8>, ContainerError> {
    // An empty message is a baseline table plus EOF, with zero chunks.
    if data.is_empty() {
        return write_container(table.bound(), alphabet, 0, std::slice::from_ref(table), &[]);
    }
    let symbols: Vec<u32> = data.iter().map(|&b| b as u32).collect();
    let chunks = vec![ChunkSpec { epoch: 0, symbols }];
    write_container(
        table.bound(),
        alphabet,
        0,
        std::slice::from_ref(table),
        &chunks,
    )
}

/// Encode bytes using the adaptive model, starting a new epoch whenever a
/// rescale fires (so rescale points match exactly on encode/decode).
pub fn encode_adaptive(
    data: &[u8],
    alphabet: u32,
    bound: u32,
    chunk_target: u32,
) -> Result<Vec<u8>, ContainerError> {
    let mut model = AdaptiveModel::new(alphabet, bound)?;
    let mut tables = vec![model.snapshot()];
    let mut chunks: Vec<ChunkSpec> = Vec::new();
    let mut current: Vec<u32> = Vec::new();

    for &b in data {
        let s = b as u32;
        if let ObserveOutcome::Rescaled { .. } =
            model.observe(s).map_err(ContainerError::BadTable)?
        {
            // Close the current chunk at the rescale boundary, publish the
            // new table epoch, then open a fresh chunk.
            if !current.is_empty() {
                chunks.push(ChunkSpec {
                    epoch: tables.len() as u32 - 1,
                    symbols: std::mem::take(&mut current),
                });
            }
            tables.push(model.snapshot());
            current.push(s);
            continue;
        }
        current.push(s);
        // Periodic chunk boundaries keep chunks independently addressable;
        // they never change the table in force.
        if chunk_target > 0 && current.len() as u32 >= chunk_target {
            chunks.push(ChunkSpec {
                epoch: tables.len() as u32 - 1,
                symbols: std::mem::take(&mut current),
            });
        }
    }
    if !current.is_empty() {
        chunks.push(ChunkSpec {
            epoch: tables.len() as u32 - 1,
            symbols: current,
        });
    }
    // Empty messages are legal: baseline table + EOF, zero chunks.
    write_container(bound, alphabet, FLAG_ADAPTIVE, &tables, &chunks)
}

/// A chunk plus the epoch whose table codes it.
#[derive(Debug)]
struct ChunkSpec {
    epoch: u32,
    symbols: Vec<u32>,
}

fn write_container(
    bound: u32,
    alphabet: u32,
    flags: u16,
    tables: &[FreqTable],
    chunks: &[ChunkSpec],
) -> Result<Vec<u8>, ContainerError> {
    if bound == 0 || bound > MAX_BOUND {
        return Err(ContainerError::BadHeader("bound out of range"));
    }
    if alphabet == 0 || alphabet > MAX_ALPHABET {
        return Err(ContainerError::BadHeader("alphabet out of range"));
    }
    if flags & !KNOWN_FLAGS != 0 {
        return Err(ContainerError::UnknownFlags { flags });
    }
    if tables.is_empty() {
        return Err(ContainerError::BadHeader("at least one table required"));
    }
    for (i, t) in tables.iter().enumerate() {
        if t.bound() != bound {
            return Err(ContainerError::BadHeader(
                "table bound disagrees with header",
            ));
        }
        if t.len() as u32 != alphabet {
            return Err(ContainerError::BadHeader(
                "table alphabet disagrees with header",
            ));
        }
        let _ = i;
    }

    let declared: u64 = chunks.iter().map(|c| c.symbols.len() as u64).sum();

    let mut out = Vec::new();
    out.extend_from_slice(&MAGIC);
    out.write_u16(FORMAT_VERSION);
    out.write_u16(flags);
    out.write_u32(bound);
    out.write_u32(alphabet);
    out.write_u64(declared);
    let header_crc = crc32(&out);
    out.write_u32(header_crc);
    debug_assert_eq!(out.len(), HEADER_LEN);

    for (epoch, table) in tables.iter().enumerate() {
        let mut body = Vec::new();
        body.write_u32(table.len() as u32);
        for &f in table.frequencies() {
            body.write_u32(f);
        }
        out.push(SEG_TABLE);
        out.write_u32(body.len() as u32);
        let frame_crc = crc32(&body);
        out.extend_from_slice(&body);
        out.write_u32(frame_crc);
        let _ = epoch;
    }

    let mut expected_offset: u64 = 0;
    for chunk in chunks {
        if chunk.symbols.is_empty() {
            return Err(ContainerError::ZeroSymbolChunk);
        }
        let table = tables
            .get(chunk.epoch as usize)
            .ok_or(ContainerError::UnknownEpoch { epoch: chunk.epoch })?;
        let payload =
            RangeEncoder::encode_vec(table, &chunk.symbols).map_err(ContainerError::from)?;

        let mut body = Vec::new();
        body.write_u64(expected_offset);
        body.write_u32(chunk.epoch);
        body.write_u32(chunk.symbols.len() as u32);
        body.write_u32(payload.len() as u32);
        body.extend_from_slice(&payload);
        out.push(SEG_CHUNK);
        out.write_u32(body.len() as u32);
        out.extend_from_slice(&body);
        out.write_u32(crc32(&body));

        expected_offset += chunk.symbols.len() as u64;
    }

    let mut eof_body = Vec::new();
    eof_body.write_u64(declared);
    out.push(SEG_EOF);
    out.write_u32(eof_body.len() as u32);
    out.extend_from_slice(&eof_body);
    out.write_u32(crc32(&eof_body));

    Ok(out)
}

// ---------------------------------------------------------------------------
// Reading
// ---------------------------------------------------------------------------

fn take_u32_len(v: u64, what: &'static str) -> Result<usize, ContainerError> {
    if v > MAX_FRAME_LEN {
        return Err(ContainerError::LengthOverflow { what, value: v });
    }
    Ok(v as usize)
}

/// Parse, validate and fully decode a container.
///
/// `budgets` bounds declared lengths *before* any payload work happens, so a
/// malicious header cannot force a huge allocation.
pub fn decode_container(
    bytes: &[u8],
    budgets: &Budgets,
) -> Result<ParsedContainer, ContainerError> {
    if bytes.len() as u64 > budgets.max_bytes {
        return Err(ContainerError::BudgetExceeded {
            declared: bytes.len() as u64,
            budget: budgets.max_bytes,
        });
    }
    if bytes.len() < HEADER_LEN {
        return Err(ContainerError::TruncatedContainer {
            segment: 0,
            needed: HEADER_LEN,
            available: bytes.len(),
        });
    }
    if bytes[0..4] != MAGIC {
        return Err(ContainerError::BadMagic);
    }
    let declared_hdr_crc = u32::from_be_bytes(bytes[24..28].try_into().unwrap());
    let computed_hdr_crc = crc32(&bytes[0..24]);
    if declared_hdr_crc != computed_hdr_crc {
        return Err(ContainerError::HeaderCrcMismatch {
            declared: declared_hdr_crc,
            computed: computed_hdr_crc,
        });
    }

    let mut hdr = BeReader::new(&bytes[4..24]);
    let version = hdr.read_u16().unwrap();
    if version != FORMAT_VERSION {
        return Err(ContainerError::UnsupportedVersion { version });
    }
    let flags = hdr.read_u16().unwrap();
    if flags & !KNOWN_FLAGS != 0 {
        return Err(ContainerError::UnknownFlags { flags });
    }
    let bound = hdr.read_u32().unwrap();
    if bound == 0 || bound > MAX_BOUND {
        return Err(ContainerError::BadHeader("bound out of range"));
    }
    let alphabet = hdr.read_u32().unwrap();
    if alphabet == 0 || alphabet > MAX_ALPHABET || alphabet > budgets.max_alphabet {
        return Err(ContainerError::BadHeader("alphabet out of range"));
    }
    let declared_symbols = hdr.read_u64().unwrap();
    if declared_symbols > budgets.max_symbols {
        return Err(ContainerError::BudgetExceeded {
            declared: declared_symbols,
            budget: budgets.max_symbols,
        });
    }

    let mut tables: Vec<FreqTable> = Vec::new();
    let mut chunks: Vec<Chunk> = Vec::new();
    let mut symbols_total: Vec<u32> = Vec::with_capacity(declared_symbols.min(1 << 20) as usize);

    let mut r = BeReader::new(&bytes[HEADER_LEN..]);
    let mut segment_no: u64 = 0;
    let mut saw_baseline = false;
    let mut saw_eof = false;
    let mut expected_next_epoch: u32 = 0;
    let mut observed_symbols: u64 = 0;

    loop {
        let frame_start = HEADER_LEN + r.position();
        let Some(marker) = r.read_u8() else {
            break; // EOF of the file; missing-EOF checked below
        };
        let frame_len = match r.read_u32() {
            Some(l) => l as u64,
            None => {
                return Err(ContainerError::TruncatedContainer {
                    segment: segment_no,
                    needed: 4,
                    available: r.remaining(),
                })
            }
        };
        let body_len = take_u32_len(frame_len, "frame body")?;
        if body_len + 4 > r.remaining() {
            return Err(ContainerError::TruncatedContainer {
                segment: segment_no,
                needed: body_len + 4,
                available: r.remaining(),
            });
        }
        let body = r.read_bytes(body_len).unwrap();
        let declared_crc = r.read_u32().unwrap();
        let computed_crc = crc32(body);
        if declared_crc != computed_crc {
            return Err(ContainerError::FrameCrcMismatch {
                segment: segment_no,
                declared: declared_crc,
                computed: computed_crc,
            });
        }

        match marker {
            SEG_TABLE => {
                let mut br = BeReader::new(body);
                let entries =
                    br.read_u32()
                        .ok_or(ContainerError::BadTable(TableError::LengthMismatch {
                            declared: 0,
                            given: 0,
                        }))?;
                if entries != alphabet {
                    return Err(ContainerError::BadTable(TableError::LengthMismatch {
                        declared: alphabet as usize,
                        given: entries as usize,
                    }));
                }
                if entries > budgets.max_alphabet {
                    return Err(ContainerError::BudgetExceeded {
                        declared: entries as u64,
                        budget: budgets.max_alphabet as u64,
                    });
                }
                let mut freqs = Vec::with_capacity(entries as usize);
                for _ in 0..entries {
                    match br.read_u32() {
                        Some(f) => freqs.push(f),
                        None => {
                            return Err(ContainerError::TruncatedContainer {
                                segment: segment_no,
                                needed: 4,
                                available: br.remaining(),
                            })
                        }
                    }
                }
                if br.remaining() != 0 {
                    return Err(ContainerError::BadHeader("TABLE frame has trailing bytes"));
                }
                // Epochs are TABLE segments numbered from 0; the first
                // segment in the container must be epoch 0.
                if tables.len() as u32 != expected_next_epoch {
                    return Err(ContainerError::BadEpoch {
                        previous: expected_next_epoch.saturating_sub(1),
                        got: tables.len() as u32,
                    });
                }
                if !saw_baseline && segment_no != 0 {
                    return Err(ContainerError::MissingBaselineTable);
                }
                saw_baseline = true;
                let table = FreqTable::new(&freqs, bound)?;
                tables.push(table);
                expected_next_epoch += 1;
            }
            SEG_CHUNK => {
                if tables.is_empty() {
                    return Err(ContainerError::MissingBaselineTable);
                }
                let mut br = BeReader::new(body);
                let start_symbol = br.read_u64().ok_or(ContainerError::BadHeader(
                    "CHUNK frame missing start offset",
                ))?;
                let epoch = br
                    .read_u32()
                    .ok_or(ContainerError::BadHeader("CHUNK frame missing epoch"))?;
                let n_symbols = br.read_u32().ok_or(ContainerError::BadHeader(
                    "CHUNK frame missing symbol count",
                ))? as u64;
                let payload_len = br.read_u32().ok_or(ContainerError::BadHeader(
                    "CHUNK frame missing payload length",
                ))? as usize;
                if n_symbols == 0 {
                    return Err(ContainerError::ZeroSymbolChunk);
                }
                if start_symbol != observed_symbols {
                    return Err(ContainerError::OutOfOrderChunks {
                        previous_end: observed_symbols,
                        chunk_start: start_symbol,
                    });
                }
                if observed_symbols + n_symbols > declared_symbols {
                    return Err(ContainerError::SymbolCountMismatch {
                        declared: declared_symbols,
                        observed: observed_symbols + n_symbols,
                    });
                }
                let payload =
                    br.read_bytes(payload_len)
                        .ok_or(ContainerError::TruncatedContainer {
                            segment: segment_no,
                            needed: payload_len,
                            available: br.remaining(),
                        })?;
                if br.remaining() != 0 {
                    return Err(ContainerError::BadHeader("CHUNK frame has trailing bytes"));
                }
                let table = tables
                    .get(epoch as usize)
                    .ok_or(ContainerError::UnknownEpoch { epoch })?;
                let decoded =
                    decode_vec(table, payload, n_symbols as usize).map_err(|e| match e {
                        DecodeError::TruncatedStream { .. } => ContainerError::TruncatedContainer {
                            segment: segment_no,
                            needed: payload_len.saturating_add(1),
                            available: payload_len,
                        },
                        other => ContainerError::BadStream(other),
                    })?;

                let frame_end = HEADER_LEN + r.position();
                chunks.push(Chunk {
                    epoch,
                    symbols: decoded.clone(),
                    frame_start,
                    frame_end,
                });
                symbols_total.extend_from_slice(&decoded);
                observed_symbols += n_symbols;
            }
            SEG_EOF => {
                let mut br = BeReader::new(body);
                let final_count = br
                    .read_u64()
                    .ok_or(ContainerError::BadHeader("EOF frame missing symbol count"))?;
                if br.remaining() != 0 {
                    return Err(ContainerError::BadHeader("EOF frame has trailing bytes"));
                }
                if final_count != declared_symbols {
                    return Err(ContainerError::EofCountMismatch {
                        header: declared_symbols,
                        eof: final_count,
                    });
                }
                saw_eof = true;
                if r.remaining() != 0 {
                    return Err(ContainerError::TrailingBytesAfterEof {
                        count: r.remaining(),
                    });
                }
                break;
            }
            other => {
                return Err(ContainerError::UnknownSegmentType {
                    segment: segment_no,
                    marker: other,
                })
            }
        }
        segment_no += 1;
    }

    if !saw_baseline {
        return Err(ContainerError::MissingBaselineTable);
    }
    if !saw_eof {
        return Err(ContainerError::MissingEof);
    }
    if observed_symbols != declared_symbols {
        return Err(ContainerError::SymbolCountMismatch {
            declared: declared_symbols,
            observed: observed_symbols,
        });
    }

    Ok(ParsedContainer {
        flags,
        bound,
        alphabet,
        declared_symbols,
        tables,
        chunks,
        symbols: symbols_total,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn byte_table(data: &[u8]) -> FreqTable {
        let mut freqs = vec![0u32; 256];
        for &b in data {
            freqs[b as usize] += 1;
        }
        for f in freqs.iter_mut() {
            if *f == 0 {
                *f = 1; // keep every byte encodable
            }
        }
        FreqTable::new(&freqs, 1 << 16).unwrap()
    }

    /// Find a segment marker by scanning frame structure (not naive byte
    /// search, which could match inside payloads).
    fn find_marker(blob: &[u8], marker: u8, mut pos: usize) -> Option<usize> {
        loop {
            if pos >= blob.len() {
                return None;
            }
            let m = blob[pos];
            let len = u32::from_be_bytes(blob.get(pos + 1..pos + 5)?.try_into().ok()?) as usize;
            if m == marker {
                return Some(pos);
            }
            pos += 1 + 4 + len + 4;
        }
    }

    #[test]
    fn static_roundtrip_short_message() {
        let msg = b"hello range coder";
        let table = byte_table(msg);
        let blob = encode_static(msg, &table, 256).unwrap();
        let parsed = decode_container(&blob, &Budgets::default()).unwrap();
        assert_eq!(parsed.symbols.len(), msg.len());
        assert_eq!(
            parsed.symbols.iter().map(|s| *s as u8).collect::<Vec<_>>(),
            msg
        );
    }

    #[test]
    fn adaptive_roundtrip_with_rescale() {
        // alphabet 256 requires bound >= 512 for rescale headroom; a small
        // bound plus a 4000-symbol body forces several rescales/epochs.
        let mut msg: Vec<u8> = Vec::with_capacity(4000);
        for k in 0..2000u32 {
            msg.push(b"abracadabra"[(k % 11) as usize]);
        }
        for k in 0..2000u32 {
            msg.push((k.wrapping_mul(2_654_435_761) >> 24) as u8);
        }
        let blob = encode_adaptive(&msg, 256, 512, 64).unwrap();
        let parsed = decode_container(&blob, &Budgets::default()).unwrap();
        assert!(parsed.flags & FLAG_ADAPTIVE != 0);
        assert!(parsed.tables.len() > 1, "small bound must force >1 epoch");
        assert!(parsed.chunks.len() > 1);
        let out: Vec<u8> = parsed.symbols.iter().map(|s| *s as u8).collect();
        assert_eq!(out, msg);
    }

    #[test]
    fn empty_message_roundtrip() {
        // Empty message: baseline table + EOF, zero chunks.
        let table = FreqTable::uniform(256, 1 << 16).unwrap();
        let blob = encode_static(b"", &table, 256).unwrap();
        let parsed = decode_container(&blob, &Budgets::default()).unwrap();
        assert_eq!(parsed.declared_symbols, 0);
        assert!(parsed.symbols.is_empty());
    }

    #[test]
    fn single_byte_roundtrip() {
        let blob = encode_adaptive(&[42], 256, 1 << 14, 0).unwrap();
        let parsed = decode_container(&blob, &Budgets::default()).unwrap();
        assert_eq!(parsed.symbols, vec![42]);
    }

    #[test]
    fn truncated_byte_is_an_error() {
        let blob = encode_adaptive(b"hello", 256, 1 << 14, 0).unwrap();
        let cut = blob.len() - 7;
        let err = decode_container(&blob[..cut], &Budgets::default()).unwrap_err();
        assert!(
            matches!(
                err,
                ContainerError::TruncatedContainer { .. } | ContainerError::MissingEof
            ),
            "got {err:?}"
        );
    }

    #[test]
    fn corrupt_payload_detected_via_crc() {
        let blob = encode_adaptive(
            b"hello world this is a longer body for crc coverage",
            256,
            1 << 14,
            0,
        )
        .unwrap();
        let mut bad = blob.clone();
        // Locate the first CHUNK marker and flip a byte well inside its body.
        let chunk_pos = find_marker(&blob, SEG_CHUNK, HEADER_LEN).unwrap();
        let flip = (chunk_pos + 12).min(bad.len().saturating_sub(6));
        bad[flip] ^= 0x01;
        let err = decode_container(&bad, &Budgets::default()).unwrap_err();
        assert!(
            matches!(
                err,
                ContainerError::FrameCrcMismatch { .. } | ContainerError::BadStream(_)
            ),
            "got {err:?}"
        );
    }

    #[test]
    fn header_crc_flip_detected() {
        let blob = encode_adaptive(b"hi", 256, 1 << 14, 0).unwrap();
        let mut bad = blob.clone();
        bad[6] ^= 0x80; // flags field, covered by the header CRC
        let err = decode_container(&bad, &Budgets::default()).unwrap_err();
        assert!(matches!(err, ContainerError::HeaderCrcMismatch { .. }));
    }

    #[test]
    fn bad_magic_detected() {
        let blob = encode_adaptive(b"hi", 256, 1 << 14, 0).unwrap();
        let mut bad = blob.clone();
        bad[0] = b'X'; // magic byte — checked before CRC
        let err = decode_container(&bad, &Budgets::default()).unwrap_err();
        assert!(matches!(err, ContainerError::BadMagic));
    }

    #[test]
    fn symbol_budget_enforced_from_header() {
        let msg = b"hello there, budget test body";
        let blob = encode_adaptive(msg, 256, 1 << 14, 0).unwrap();
        let budgets = Budgets {
            max_symbols: 1,
            ..Budgets::default()
        };
        let err = decode_container(&blob, &budgets).unwrap_err();
        assert!(
            matches!(err, ContainerError::BudgetExceeded { declared, budget } if declared == msg.len() as u64 && budget == 1),
            "got {err:?}"
        );
    }

    #[test]
    fn byte_budget_enforced_before_parse() {
        let blob = encode_adaptive(b"hi", 256, 1 << 14, 0).unwrap();
        let budgets = Budgets {
            max_bytes: (blob.len() - 1) as u64,
            ..Budgets::default()
        };
        assert!(matches!(
            decode_container(&blob, &budgets).unwrap_err(),
            ContainerError::BudgetExceeded { .. }
        ));
    }

    #[test]
    fn invalid_bound_rejected() {
        let err = encode_adaptive(b"abc", 256, MAX_BOUND + 1, 0).unwrap_err();
        assert!(matches!(
            err,
            ContainerError::BadTable(_) | ContainerError::BadHeader(_)
        ));
    }
}
