//! Diagnostics: request identifiers and decision records.
//!
//! Every query produces a `DecisionRecord` explaining *why* the answer
//! is member / not_member / undecidable. Keys themselves never appear in
//! logs or responses — only their masked id (see `hash::masked_key_id`).

use serde::Serialize;

/// Request id attached to every response and log line.
#[derive(Debug, Clone)]
pub struct RequestId(pub String);

impl RequestId {
    pub fn new(counter: u64) -> Self {
        let millis = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        Self(format!("req-{:013x}-{:06x}", millis, counter))
    }
}

#[derive(Debug, Serialize)]
pub struct DecisionRecord {
    pub request_id: String,
    /// Masked key identity: `fp12=<hex>,len=<n>`. No key material.
    pub key_id: String,
    pub decision: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub slot: Option<u64>,
    pub reason: String,
    /// Seed of the index that answered, when one is loaded.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub index_seed: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub index_n: Option<usize>,
}
