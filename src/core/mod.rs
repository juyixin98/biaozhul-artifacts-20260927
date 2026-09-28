//! Format kernel: errors, checksums, constants, wire format, codec.
//!
//! This crate is deliberately layered:
//!
//! ```text
//! core/*      data format + encode/decode kernel (no I/O, no async)
//! reference/  independent second decompressor (shares nothing with core)
//! store/      filesystem persistence adapter
//! http/       axum validation/service interface
//! bin/        server and CLI
//! ```

pub mod checksum;
pub mod constants;
pub mod decoder;
pub mod encoder;
pub mod error;
pub mod format;
pub mod varint;

use crate::core::decoder::ChainSession;
use crate::core::encoder::EncodeOutcome;
use crate::core::error::Result;

/// Chunk `input` into fixed-size pieces (last piece may be short).
pub fn fixed_chunks(input: &[u8], chunk_size: usize) -> impl Iterator<Item = &[u8]> {
    assert!(chunk_size > 0, "chunk size must be positive");
    input.chunks(chunk_size)
}

/// Encode one input as a chain: block 0 independent, all later blocks
/// dependent on the rolling dictionary. Returns raw blocks in order.
pub fn encode_chain(input: &[u8], chunk_size: usize) -> Result<Vec<EncodeOutcome>> {
    let mut session = ChainSession::new();
    let mut blocks = Vec::new();
    for piece in fixed_chunks(input, chunk_size) {
        let outcome = decoder::encode_next(&session, piece)?;
        // Roll the session's dictionary exactly like a successful decode would,
        // so the next block binds the correct digest. We reuse the decoder
        // rather than duplicating the rollover rule.
        session.decode_raw(&outcome.raw)?;
        blocks.push(outcome);
    }
    Ok(blocks)
}

/// Decode a chain of raw blocks end-to-end, failing on the first invalid
/// block. Used by the "different chunking modes recover identical bytes"
/// evidence tests.
pub fn decode_chain(blocks: &[Vec<u8>]) -> Result<Vec<u8>> {
    let mut session = ChainSession::new();
    let mut all = Vec::new();
    for raw in blocks {
        let part = session.decode_raw(raw)?;
        all.extend_from_slice(&part);
    }
    Ok(all)
}
