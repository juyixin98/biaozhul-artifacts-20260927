//! Runtime configuration, loaded from optional TOML files and environment
//! overrides. Defaults let the service run with zero configuration.

use crate::container::DecodeBudget;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct Config {
    pub listen: String,
    pub data_dir: PathBuf,
    pub log_level: String,
    pub http_body_limit: usize,
    pub budget: DecodeBudget,
    /// Redact input payloads in diagnostic logs (on by default; payloads are
    /// treated as potentially sensitive).
    pub redact_payloads: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            listen: "127.0.0.1:8080".into(),
            data_dir: PathBuf::from("./data"),
            log_level: "info".into(),
            http_body_limit: 8 << 20, // 8 MiB
            budget: DecodeBudget::default(),
            redact_payloads: true,
        }
    }
}

impl Config {
    /// Load defaults, overlay a TOML file if present, then environment
    /// variables (`RANGE_CODEC_LISTEN`, `RANGE_CODEC_DATA_DIR`,
    /// `RANGE_CODEC_LOG`, `RANGE_CODEC_REDACT=0|1`).
    pub fn load(path: Option<&std::path::Path>) -> Result<Self, String> {
        let mut cfg = Config::default();

        if let Some(path) = path {
            let text = std::fs::read_to_string(path)
                .map_err(|e| format!("read config {}: {e}", path.display()))?;
            let v: toml::Value = text
                .parse::<toml::Value>()
                .map_err(|e| format!("parse config: {e}"))?;
            if let Some(s) = v.get("listen").and_then(|x| x.as_str()) {
                cfg.listen = s.into();
            }
            if let Some(s) = v.get("data_dir").and_then(|x| x.as_str()) {
                cfg.data_dir = PathBuf::from(s);
            }
            if let Some(s) = v.get("log_level").and_then(|x| x.as_str()) {
                cfg.log_level = s.into();
            }
            if let Some(n) = v.get("http_body_limit").and_then(|x| x.as_integer()) {
                cfg.http_body_limit = n.max(0) as usize;
            }
            if let Some(b) = v.get("redact_payloads").and_then(|x| x.as_bool()) {
                cfg.redact_payloads = b;
            }
            if let Some(bud) = v.get("budget").and_then(|x| x.as_table()) {
                if let Some(n) = bud.get("max_symbol_count").and_then(|x| x.as_integer()) {
                    cfg.budget.max_symbol_count = n.max(0) as u64;
                }
                if let Some(n) = bud.get("max_chunks").and_then(|x| x.as_integer()) {
                    cfg.budget.max_chunks = n.max(0) as usize;
                }
                if let Some(n) = bud.get("max_payload_bytes").and_then(|x| x.as_integer()) {
                    cfg.budget.max_payload_bytes = n.max(0) as usize;
                }
                if let Some(n) = bud.get("max_chunk_symbols").and_then(|x| x.as_integer()) {
                    cfg.budget.max_chunk_symbols = n.max(0) as u32;
                }
            }
        }

        if let Ok(v) = std::env::var("RANGE_CODEC_LISTEN") {
            cfg.listen = v;
        }
        if let Ok(v) = std::env::var("RANGE_CODEC_DATA_DIR") {
            cfg.data_dir = PathBuf::from(v);
        }
        if let Ok(v) = std::env::var("RANGE_CODEC_LOG") {
            cfg.log_level = v;
        }
        if let Ok(v) = std::env::var("RANGE_CODEC_REDACT") {
            cfg.redact_payloads = v != "0";
        }
        Ok(cfg)
    }
}
