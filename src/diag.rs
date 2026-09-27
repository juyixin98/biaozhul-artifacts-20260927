//! Diagnostics: every request/result carries a correlation id and enough
//! state to explain why it was accepted, rejected, or left undecided.
//!
//! Anything tagged as sensitive (client-provided labels, descriptions,
//! opaque tokens) is rendered through [`redact`] so log lines never contain
//! raw secret material. Boolean expressions and variable names from the
//! synthetic fixtures are not secret and are shown in full.

use std::sync::atomic::{AtomicU64, Ordering};

use serde::Serialize;

static REQUEST_SEQ: AtomicU64 = AtomicU64::new(1);

/// Generate a short correlation id unique within this server process.
pub fn new_request_id() -> String {
    format!("req-{:012x}", REQUEST_SEQ.fetch_add(1, Ordering::Relaxed))
}

/// Replace a sensitive string with a length-revealing, content-hiding token.
///
/// ```text
/// "super-secret" -> "redacted(12)"
/// ""             -> "redacted(0)"
/// ```
pub fn redact(secret: &str) -> String {
    format!("redacted({})", secret.chars().count())
}

/// The outcome category used in diagnostics.
#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Accepted,
    Rejected,
    Error,
    Inconclusive,
}

/// A structured diagnostic attached to API responses and log lines.
#[derive(Clone, Debug, Serialize)]
pub struct Diag {
    pub request_id: String,
    pub outcome: Outcome,
    /// Stable machine-readable code (`ok`, an error kind, …).
    pub code: String,
    /// One-line human explanation of the decision.
    pub reason: String,
    /// Relevant non-sensitive state (node counts, limits, epochs…).
    pub state: serde_json::Value,
    /// Sensitive inputs are represented only as redaction markers.
    pub sensitive: serde_json::Value,
}

impl Diag {
    pub fn ok(request_id: impl Into<String>, reason: impl Into<String>) -> Self {
        Diag {
            request_id: request_id.into(),
            outcome: Outcome::Accepted,
            code: "ok".into(),
            reason: reason.into(),
            state: serde_json::json!({}),
            sensitive: serde_json::json!({}),
        }
    }

    pub fn rejected(request_id: impl Into<String>, reason: impl Into<String>) -> Self {
        Diag {
            request_id: request_id.into(),
            outcome: Outcome::Rejected,
            code: "not-equivalent".into(),
            reason: reason.into(),
            state: serde_json::json!({}),
            sensitive: serde_json::json!({}),
        }
    }

    pub fn error(
        request_id: impl Into<String>,
        code: impl Into<String>,
        reason: impl Into<String>,
    ) -> Self {
        Diag {
            request_id: request_id.into(),
            outcome: Outcome::Error,
            code: code.into(),
            reason: reason.into(),
            state: serde_json::json!({}),
            sensitive: serde_json::json!({}),
        }
    }

    pub fn with_state(mut self, state: serde_json::Value) -> Self {
        self.state = state;
        self
    }

    pub fn with_sensitive(mut self, fields: serde_json::Value) -> Self {
        self.sensitive = fields;
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_ids_are_unique() {
        assert_ne!(new_request_id(), new_request_id());
    }

    #[test]
    fn redaction_hides_content_and_reveals_length() {
        assert_eq!(redact("super-secret"), "redacted(12)");
        assert_eq!(redact(""), "redacted(0)");
        // Unicode counts characters, not bytes.
        assert_eq!(redact("秘"), "redacted(1)");
        // Never echoes the secret.
        assert!(!redact("hunter2").contains("hunter2"));
    }
}
