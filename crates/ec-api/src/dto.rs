//! HTTP request/response DTOs. Base64 (standard alphabet) is used for all
//! binary payloads so requests stay plain JSON.

use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
pub struct EncodeRequest {
    /// Object id; a UUID v4 is generated when omitted.
    #[serde(default)]
    pub object_id: Option<String>,
    /// Raw object bytes, standard base64 (padding `=` allowed/omitted).
    pub data_b64: String,
}

#[derive(Debug, Deserialize)]
pub struct DecodeRequest {
    /// Store mode: fetch shards from the configured storage.
    #[serde(default)]
    pub object_id: Option<String>,
    /// Stateless mode: the manifest JSON exactly as produced by encode.
    #[serde(default)]
    pub manifest_json: Option<serde_json::Value>,
    /// Stateless mode: shards the caller has.
    #[serde(default)]
    pub shards: Vec<ShardInput>,
    /// When false the response reports recoverability but omits data bytes.
    #[serde(default = "default_true")]
    pub include_data: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ShardInput {
    pub index: u16,
    pub data_b64: String,
}

#[derive(Debug, Deserialize)]
pub struct RepairRequest {
    pub object_id: String,
    /// Shard indices to rebuild; missing/bad ones among them are repaired.
    #[serde(default)]
    pub targets: Vec<u16>,
}

fn default_true() -> bool {
    true
}
