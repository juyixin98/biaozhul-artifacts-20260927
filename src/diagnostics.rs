//! Structured diagnostics: every accept/reject decision carries a request id,
//! the reason, and key state.  Raw symbols are never logged.

use serde::Serialize;
use std::sync::atomic::{AtomicU64, Ordering};

static REQUEST_SEQ: AtomicU64 = AtomicU64::new(1);

/// Generate a short process-unique request id, e.g. `req-000042`.
pub fn new_request_id() -> String {
    let n = REQUEST_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("req-{n:06}")
}

/// A fingerprint safe to print for arbitrary payloads: length plus the first
/// and last few bytes shown as hex, middle elided.  Never prints full input.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct PayloadFingerprint {
    pub len: usize,
    pub crc32: u32,
    pub head_hex: String,
    pub tail_hex: String,
}

const FINGERPRINT_WINDOW: usize = 8;

impl PayloadFingerprint {
    pub fn of(bytes: &[u8]) -> Self {
        let hex = |b: &[u8]| -> String { b.iter().map(|x| format!("{x:02x}")).collect() };
        let (head, tail) = if bytes.len() <= 2 * FINGERPRINT_WINDOW {
            (bytes, &[][..])
        } else {
            (
                &bytes[..FINGERPRINT_WINDOW],
                &bytes[bytes.len() - FINGERPRINT_WINDOW..],
            )
        };
        Self {
            len: bytes.len(),
            crc32: crate::format::crc32(bytes),
            head_hex: hex(head),
            tail_hex: hex(tail),
        }
    }
}

/// The verdict associated with one operation.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    /// Input satisfied the contract; result produced.
    Accepted,
    /// Input violated the contract; `reason` says exactly how.
    Rejected,
    /// Could not decide (e.g. external storage failure).
    Indeterminate,
}

/// A structured diagnostic record.  `key_state` holds small derived numbers
/// (totals, counts, offsets), never payload content.
#[derive(Debug, Clone, Serialize)]
pub struct DiagnosticRecord {
    pub request_id: String,
    pub operation: &'static str,
    pub decision: Decision,
    pub reason: String,
    pub error_kind: Option<&'static str>,
    pub key_state: serde_json::Value,
    pub input: PayloadFingerprint,
}

impl DiagnosticRecord {
    pub fn accepted(
        operation: &'static str,
        request_id: impl Into<String>,
        input: &[u8],
        key_state: serde_json::Value,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            operation,
            decision: Decision::Accepted,
            reason: reason.into(),
            error_kind: None,
            key_state,
            input: PayloadFingerprint::of(input),
        }
    }

    pub fn rejected(
        operation: &'static str,
        request_id: impl Into<String>,
        input: &[u8],
        key_state: serde_json::Value,
        error_kind: &'static str,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            operation,
            decision: Decision::Rejected,
            reason: reason.into(),
            error_kind: Some(error_kind),
            key_state,
            input: PayloadFingerprint::of(input),
        }
    }

    pub fn indeterminate(
        operation: &'static str,
        request_id: impl Into<String>,
        input: &[u8],
        key_state: serde_json::Value,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            request_id: request_id.into(),
            operation,
            decision: Decision::Indeterminate,
            reason: reason.into(),
            error_kind: None,
            key_state,
            input: PayloadFingerprint::of(input),
        }
    }
}

/// Map any container error to a stable machine-readable kind string.
pub fn container_error_kind(e: &crate::error::ContainerError) -> &'static str {
    use crate::error::ContainerError::*;
    match e {
        BadMagic => "bad_magic",
        UnsupportedVersion { .. } => "unsupported_version",
        UnknownFlags { .. } => "unknown_flags",
        HeaderCrcMismatch { .. } => "header_crc_mismatch",
        FrameCrcMismatch { .. } => "frame_crc_mismatch",
        TruncatedContainer { .. } => "truncated_container",
        UnknownSegmentType { .. } => "unknown_segment_type",
        MissingEof => "missing_eof",
        TrailingBytesAfterEof { .. } => "trailing_bytes",
        MissingBaselineTable => "missing_baseline_table",
        BadEpoch { .. } => "bad_epoch",
        UnknownEpoch { .. } => "unknown_epoch",
        ZeroSymbolChunk => "zero_symbol_chunk",
        SymbolCountMismatch { .. } => "symbol_count_mismatch",
        EofCountMismatch { .. } => "eof_count_mismatch",
        OutOfOrderChunks { .. } => "out_of_order_chunks",
        BadTable(_) => "bad_table",
        LengthOverflow { .. } => "length_overflow",
        BudgetExceeded { .. } => "budget_exceeded",
        BadStream(_) => "bad_range_stream",
        BadEncoding(_) => "bad_symbol_for_encoding",
        BadHeader(_) => "bad_header",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprints_never_contain_middle_of_long_input() {
        let secret = b"USERSECRET-prefix-1234567890-tailMARKER!!";
        let fp = PayloadFingerprint::of(secret);
        let shown = format!("{} {}", fp.head_hex, fp.tail_hex);
        assert!(!shown.contains(&hex_word(b"USERSECRET")));
        // head shows only the first 8 bytes
        assert_eq!(fp.head_hex.len(), 16);
        assert_eq!(fp.tail_hex.len(), 16);
        assert_eq!(fp.len, secret.len());
    }

    #[test]
    fn short_inputs_shown_whole_without_tail_split() {
        let fp = PayloadFingerprint::of(b"abc");
        assert_eq!(fp.head_hex, "616263");
        assert!(fp.tail_hex.is_empty());
    }

    #[test]
    fn request_ids_are_unique() {
        let a = new_request_id();
        let b = new_request_id();
        assert_ne!(a, b);
        assert!(a.starts_with("req-"));
    }

    fn hex_word(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }
}
