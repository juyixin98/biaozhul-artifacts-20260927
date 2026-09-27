//! Configuration layer.
//!
//! Precedence (highest first): environment variables → TOML config file →
//! built-in defaults. All paths and ports stay local; no external services.

use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Config {
    /// Filesystem directory holding `events.jsonl` and `CURRENT`.
    pub data_dir: PathBuf,
    /// TCP bind address for the Axum server.
    pub bind: String,
    /// Maximum accepted JSON request body size in bytes.
    pub max_body_bytes: usize,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            data_dir: PathBuf::from("./data"),
            bind: "127.0.0.1:8080".to_string(),
            max_body_bytes: 16 * 1024 * 1024,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
struct FileConfig {
    data_dir: Option<PathBuf>,
    bind: Option<String>,
    max_body_bytes: Option<usize>,
}

impl Config {
    /// Load configuration: defaults, optional TOML file, then env overrides
    /// (`PREREG2D_DATA_DIR`, `PREREG2D_BIND`, `PREREG2D_MAX_BODY_BYTES`).
    pub fn load(config_path: Option<&Path>) -> Result<Self, String> {
        let mut cfg = Config::default();

        if let Some(path) = config_path {
            let text = std::fs::read_to_string(path)
                .map_err(|e| format!("cannot read config {}: {e}", path.display()))?;
            let file: FileConfig = toml::from_str(&text)
                .map_err(|e| format!("bad TOML in {}: {e}", path.display()))?;
            if let Some(v) = file.data_dir {
                cfg.data_dir = v;
            }
            if let Some(v) = file.bind {
                cfg.bind = v;
            }
            if let Some(v) = file.max_body_bytes {
                cfg.max_body_bytes = v;
            }
        }

        if let Ok(v) = std::env::var("PREREG2D_DATA_DIR") {
            cfg.data_dir = PathBuf::from(v);
        }
        if let Ok(v) = std::env::var("PREREG2D_BIND") {
            cfg.bind = v;
        }
        if let Ok(v) = std::env::var("PREREG2D_MAX_BODY_BYTES") {
            cfg.max_body_bytes = v
                .parse()
                .map_err(|_| format!("PREREG2D_MAX_BODY_BYTES={v:?} is not a usize"))?;
        }

        if cfg.max_body_bytes == 0 {
            return Err("max_body_bytes must be > 0".to_string());
        }
        Ok(cfg)
    }
}
