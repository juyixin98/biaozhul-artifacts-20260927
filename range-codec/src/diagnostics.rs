//! Structured diagnostics with request/record identifiers, key state and
//! sensitive-data redaction.
//!
//! A [`Diagnostic`] answers *why* an input was accepted, rejected or could
//! not be judged. It is safe to log or return over HTTP: payloads are only
//! ever represented by their length and a short fingerprint, never their raw
//! bytes.

use crate::error::{CodecError, Decision};
use serde::Serialize;
use uuid::Uuid;

/// Stable per-request id, generated if the caller supplies none.
#[derive(Debug, Clone)]
pub struct RequestId(pub String);

impl RequestId {
    pub fn new() -> Self {
        RequestId(format!("req_{}", Uuid::new_v4().simple()))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for RequestId {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Display for RequestId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// First 4 bytes of a SHA-free fingerprint: FNV-1a 64, truncated for display.
/// Good enough to correlate logs without revealing content; not a security
/// primitive.
pub fn fingerprint(bytes: &[u8]) -> String {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut h = OFFSET;
    for &b in bytes.iter().take(4096) {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    format!("{h:016x}/{}", bytes.len())
}

/// Machine- and human-readable verdict attached to every response.
#[derive(Debug, Clone, Serialize)]
pub struct Diagnostic {
    pub request_id: String,
    /// `accepted` | `rejected` | `indeterminate`.
    pub decision: String,
    pub error_code: Option<String>,
    pub message: String,
    /// Key state captured at the decision point (lengths, offsets, bounds).
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub state: Vec<(String, String)>,
    /// Non-sensitive payload descriptor (`<fnv1a>/<len>`).
    pub input: String,
}

impl Diagnostic {
    /// Build the accepted/rejected/indeterminate diagnostic.
    pub fn for_result(
        request_id: &RequestId,
        input: &[u8],
        result: &std::result::Result<(), CodecError>,
        extra: Vec<(String, String)>,
    ) -> Diagnostic {
        match result {
            Ok(()) => Diagnostic {
                request_id: request_id.0.clone(),
                decision: "accepted".into(),
                error_code: None,
                message: "input accepted".into(),
                state: extra,
                input: fingerprint(input),
            },
            Err(err) => Diagnostic::from_error(request_id, input, err, extra),
        }
    }

    pub fn from_error(
        request_id: &RequestId,
        input: &[u8],
        err: &CodecError,
        mut state: Vec<(String, String)>,
    ) -> Diagnostic {
        state.push(("decision_reason".into(), err.decision().as_str().into()));
        Diagnostic {
            request_id: request_id.0.clone(),
            decision: match err.decision() {
                Decision::Rejected => "rejected".into(),
                Decision::Indeterminate => "indeterminate".into(),
            },
            error_code: Some(err.code().into()),
            message: err.to_string(),
            state,
            input: fingerprint(input),
        }
    }

    /// Log-safe description of raw input bytes. With redaction enabled only
    /// length is printed; the non-reversible fingerprint is always safe.
    pub fn describe_payload(bytes: &[u8], redact: bool) -> String {
        if redact {
            format!("<redacted len={}>", bytes.len())
        } else {
            const HEAD: usize = 16;
            let preview: String = bytes
                .iter()
                .take(HEAD)
                .map(|b| format!("{b:02x}"))
                .collect::<Vec<_>>()
                .join(" ");
            format!("{preview}{}", if bytes.len() > HEAD { " …" } else { "" })
        }
    }
}

/// Emit a tracing event carrying the diagnostic and redacted input.
pub fn log_diagnostic(d: &Diagnostic, raw: &[u8], redact: bool) {
        tracing::info!(
        request_id = %d.request_id,
        decision = %d.decision,
        error_code = ?d.error_code,
        input_fingerprint = %d.input,
        payload = %Diagnostic::describe_payload(raw, redact),
        state = ?d.state,
        message = %d.message,
        "verdict"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redaction_never_prints_bytes() {
        let secret = b"super-secret-payload-0123456789";
        let shown = Diagnostic::describe_payload(secret, true);
        assert!(!shown.contains("secret"));
        assert!(shown.contains("len=31"));
        let shown = Diagnostic::describe_payload(secret, false);
        assert!(shown.starts_with("73 75 70 65 72 2d 73 65"));
    }

    #[test]
    fn fingerprint_is_length_sensitive() {
        assert_ne!(fingerprint(b"abc"), fingerprint(b"abd"));
        assert!(fingerprint(b"abc").ends_with("/3"));
    }

    #[test]
    fn diagnostic_classifies_reserved_flag_as_indeterminate() {
        let rid = RequestId::new();
        let err = CodecError::ReservedFlag { flag: 0x80 };
        let d = Diagnostic::from_error(&rid, &[0u8; 10], &err, vec![]);
        assert_eq!(d.decision, "indeterminate");
        assert_eq!(d.error_code.unwrap(), "RESERVED_FLAG");
        assert!(d.input.ends_with("/10"));
    }
}
