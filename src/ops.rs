//! High-level operations shared by the CLI and HTTP layers.
//!
//! Each operation returns its result plus a [`DiagnosticRecord`] describing
//! why it was accepted, rejected, or could not be judged.  The record only
//! ever contains fingerprints and small derived state — never raw symbols.

use crate::container::{decode_container, encode_adaptive, encode_static, Budgets};
use crate::diagnostics::{container_error_kind, Decision, DiagnosticRecord};
use crate::error::ContainerError;
use crate::table::FreqTable;

/// Build a static byte-alphabet table: observed frequency plus one for every
/// unseen symbol (so every byte stays encodable).  Errors if the total
/// exceeds `bound`.
pub fn byte_frequency_table(data: &[u8], bound: u32) -> Result<FreqTable, ContainerError> {
    let mut freqs = [1u32; 256];
    for &b in data {
        freqs[b as usize] += 1;
    }
    FreqTable::new(&freqs, bound).map_err(ContainerError::BadTable)
}

/// Outcome of an encode request.
pub struct EncodeResult {
    pub container: Vec<u8>,
    pub declared_symbols: u64,
    pub chunks: usize,
    pub table_epochs: usize,
}

/// Encode raw bytes.  `mode`: "static" uses one table; "adaptive" emits a new
/// epoch on every rescale.
pub fn encode_bytes(
    request_id: &str,
    data: &[u8],
    mode: EncodeMode,
    bound: u32,
    chunk_target: u32,
) -> (Result<EncodeResult, ContainerError>, DiagnosticRecord) {
    let result = (|| -> Result<EncodeResult, ContainerError> {
        let container = match mode {
            EncodeMode::Static => {
                let table = byte_frequency_table(data, bound)?;
                let epochs = 1usize;
                let blob = encode_static(data, &table, 256)?;
                // Re-parse count fields cheaply via header math: chunk count is
                // 1 for non-empty static, 0 for empty.
                let chunks = if data.is_empty() { 0 } else { 1 };
                EncodeResult {
                    container: blob,
                    declared_symbols: data.len() as u64,
                    chunks,
                    table_epochs: epochs,
                }
            }
            EncodeMode::Adaptive => {
                let blob = encode_adaptive(data, 256, bound, chunk_target)?;
                // Parse to report honest structural counts.
                let parsed = decode_container(
                    &blob,
                    &Budgets {
                        max_alphabet: 256,
                        ..Budgets::default()
                    },
                )?;
                EncodeResult {
                    container: blob,
                    declared_symbols: parsed.declared_symbols,
                    chunks: parsed.chunks.len(),
                    table_epochs: parsed.tables.len(),
                }
            }
        };
        Ok(container)
    })();

    let record = match &result {
        Ok(r) => DiagnosticRecord::accepted(
            "encode",
            request_id,
            data,
            serde_json::json!({
                "mode": mode.as_str(),
                "bound": bound,
                "declared_symbols": r.declared_symbols,
                "container_bytes": r.container.len(),
                "chunks": r.chunks,
                "table_epochs": r.table_epochs,
            }),
            format!(
                "{} symbols encoded into {} bytes across {} chunk(s), {} table epoch(s)",
                r.declared_symbols,
                r.container.len(),
                r.chunks,
                r.table_epochs
            ),
        ),
        Err(e) => DiagnosticRecord::rejected(
            "encode",
            request_id,
            data,
            serde_json::json!({ "mode": mode.as_str(), "bound": bound }),
            container_error_kind(e),
            e.to_string(),
        ),
    };
    (result, record)
}

/// Outcome of a decode request.
pub struct DecodeResult {
    pub data: Vec<u8>,
    pub declared_symbols: u64,
    pub chunks: usize,
    pub table_epochs: usize,
}

/// Validate and decode a container under explicit resource budgets.
pub fn decode_bytes(
    request_id: &str,
    container: &[u8],
    budgets: &Budgets,
) -> (Result<DecodeResult, ContainerError>, DiagnosticRecord) {
    let result = decode_container(container, budgets).map(|parsed| DecodeResult {
        data: parsed.symbols.iter().map(|&s| s as u8).collect(),
        declared_symbols: parsed.declared_symbols,
        chunks: parsed.chunks.len(),
        table_epochs: parsed.tables.len(),
    });

    let key_state = serde_json::json!({
        "container_bytes": container.len(),
        "budget_symbols": budgets.max_symbols,
        "budget_bytes": budgets.max_bytes,
    });
    let record = match &result {
        Ok(r) => DiagnosticRecord::accepted(
            "decode",
            request_id,
            container,
            {
                let mut v = key_state.clone();
                v["declared_symbols"] = r.declared_symbols.into();
                v["chunks"] = r.chunks.into();
                v["table_epochs"] = r.table_epochs.into();
                v
            },
            format!(
                "container accepted: {} symbols from {} chunk(s), {} table epoch(s)",
                r.declared_symbols, r.chunks, r.table_epochs
            ),
        ),
        Err(e) => DiagnosticRecord::rejected(
            "decode",
            request_id,
            container,
            key_state,
            container_error_kind(e),
            format!("container rejected ({:?}): {e}", Decision::Rejected),
        ),
    };
    (result, record)
}

/// Which model the encoder uses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EncodeMode {
    Static,
    Adaptive,
}

impl EncodeMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            EncodeMode::Static => "static",
            EncodeMode::Adaptive => "adaptive",
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "static" => Some(EncodeMode::Static),
            "adaptive" => Some(EncodeMode::Adaptive),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_decode_static_records_acceptance() {
        let id = "req-test-1";
        let (er, rec) = encode_bytes(id, b"abcabc", EncodeMode::Static, 1 << 14, 0);
        let er = er.unwrap();
        assert_eq!(rec.decision, Decision::Accepted);
        let (dr, rec2) = decode_bytes(id, &er.container, &Budgets::default());
        let dr = dr.unwrap();
        assert_eq!(dr.data, b"abcabc");
        assert_eq!(rec2.decision, Decision::Accepted);
    }

    #[test]
    fn decode_rejection_has_error_kind() {
        let mut garbage = b"RCMP".to_vec();
        garbage.extend_from_slice(&[0xff; 64]);
        let (_, rec) = decode_bytes("req-test-2", &garbage, &Budgets::default());
        assert_eq!(rec.decision, Decision::Rejected);
        // First failure with wrong magic is bad_magic; with right magic but
        // corrupt body it is a header/frame CRC failure.
        assert!(matches!(
            rec.error_kind,
            Some("header_crc_mismatch" | "truncated_container" | "bad_magic")
        ));
    }

    #[test]
    fn wrong_magic_is_bad_magic_kind() {
        // A header-length buffer whose magic is wrong: magic is the first
        // structural check, so the error kind is bad_magic.
        let garbage = vec![b'X'; 28];
        let (_, rec) = decode_bytes("req-test-3", &garbage, &Budgets::default());
        assert_eq!(rec.error_kind, Some("bad_magic"));
        assert_eq!(rec.decision, Decision::Rejected);
    }
}
