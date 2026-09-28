//! Service configuration, loaded from a JSON file with defaults.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ServiceConfig {
    /// Address the HTTP server binds to.
    pub listen: String,
    /// Default path for the persisted index.
    pub index_path: String,
    /// Base seed when a build request does not specify one.
    pub default_seed: u64,
    /// Default cap on seeded build attempts.
    pub default_max_attempts: u32,
    /// Hard cap on unique keys per build.
    pub max_keys: usize,
}

impl Default for ServiceConfig {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:8080".to_string(),
            index_path: "data/index.mph".to_string(),
            default_seed: 0x5EED_0001,
            default_max_attempts: 128,
            max_keys: 1_000_000,
        }
    }
}

impl ServiceConfig {
    pub fn load(path: &str) -> Result<Self, ConfigError> {
        match std::fs::read_to_string(path) {
            Ok(text) => serde_json::from_str(&text).map_err(|e| ConfigError::Parse {
                path: path.to_string(),
                message: e.to_string(),
            }),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Ok(Self::default())
            }
            Err(e) => Err(ConfigError::Io {
                path: path.to_string(),
                message: e.to_string(),
            }),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("cannot read config {path}: {message}")]
    Io { path: String, message: String },
    #[error("cannot parse config {path}: {message}")]
    Parse { path: String, message: String },
}
