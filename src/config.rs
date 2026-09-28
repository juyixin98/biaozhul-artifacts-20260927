//! Runtime configuration: built-in defaults, a JSON file, and environment
//! overrides, applied in that order (env wins over file wins over defaults).

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Application configuration.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct Config {
    /// HTTP listen address (Axum server).
    pub bind_addr: String,
    /// Directory the persistence adapter stores containers in.
    pub storage_dir: PathBuf,
    /// Frequency bound for the adaptive model (`1..=2**24`).
    pub frequency_bound: u32,
    /// Alphabet size used for byte messages.
    pub alphabet: u32,
    /// Symbols per chunk in adaptive mode (0 = only rescale boundaries).
    pub chunk_target: u32,
    /// Decoder resource budgets.
    pub max_symbols: u64,
    pub max_bytes: u64,
    pub max_alphabet: u32,
    /// Include payloads (base64) in API responses.  Off by default: the
    /// diagnostics layer otherwise logs only fingerprints.
    pub expose_data: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            bind_addr: "127.0.0.1:8080".to_string(),
            storage_dir: PathBuf::from("./data/jobs"),
            frequency_bound: 1 << 14,
            alphabet: 256,
            chunk_target: 4096,
            max_symbols: 1 << 28,
            max_bytes: 1 << 30,
            max_alphabet: 1 << 20,
            expose_data: false,
        }
    }
}

impl Config {
    /// Defaults + optional JSON file + `RANGECODE_*` environment overrides.
    pub fn load(file: Option<&std::path::Path>) -> Result<Self, ConfigError> {
        let mut cfg = Config::default();
        if let Some(path) = file {
            let text = std::fs::read_to_string(path).map_err(|e| ConfigError::Io {
                path: path.to_path_buf(),
                message: e.to_string(),
            })?;
            cfg = serde_json::from_str(&text).map_err(|e| ConfigError::Parse {
                path: path.to_path_buf(),
                message: e.to_string(),
            })?;
        }
        cfg.apply_env()?;
        cfg.validate()?;
        Ok(cfg)
    }

    fn apply_env(&mut self) -> Result<(), ConfigError> {
        macro_rules! env {
            ($name:expr, $field:ident, String) => {
                if let Ok(v) = std::env::var($name) {
                    self.$field = v;
                }
            };
            ($name:expr, $field:ident, PathBuf) => {
                if let Ok(v) = std::env::var($name) {
                    self.$field = PathBuf::from(v);
                }
            };
            ($name:expr, $field:ident, bool) => {
                if let Ok(v) = std::env::var($name) {
                    self.$field = matches!(v.as_str(), "1" | "true" | "yes" | "on");
                }
            };
            ($name:expr, $field:ident, $ty:ty) => {
                if let Ok(v) = std::env::var($name) {
                    self.$field = v.parse::<$ty>().map_err(|e| ConfigError::BadEnv {
                        name: $name.to_string(),
                        value: v,
                        message: e.to_string(),
                    })?;
                }
            };
        }
        env!("RANGECODE_BIND_ADDR", bind_addr, String);
        env!("RANGECODE_STORAGE_DIR", storage_dir, PathBuf);
        env!("RANGECODE_FREQUENCY_BOUND", frequency_bound, u32);
        env!("RANGECODE_ALPHABET", alphabet, u32);
        env!("RANGECODE_CHUNK_TARGET", chunk_target, u32);
        env!("RANGECODE_MAX_SYMBOLS", max_symbols, u64);
        env!("RANGECODE_MAX_BYTES", max_bytes, u64);
        env!("RANGECODE_MAX_ALPHABET", max_alphabet, u32);
        env!("RANGECODE_EXPOSE_DATA", expose_data, bool);
        Ok(())
    }

    /// Cross-field validation.
    pub fn validate(&self) -> Result<(), ConfigError> {
        use crate::table::MAX_BOUND;
        if self.frequency_bound == 0 || self.frequency_bound > MAX_BOUND {
            return Err(ConfigError::InvalidValue(format!(
                "frequency_bound must be in 1..=2**24, got {}",
                self.frequency_bound
            )));
        }
        if self.alphabet == 0 || self.alphabet > self.frequency_bound {
            return Err(ConfigError::InvalidValue(format!(
                "alphabet {} must be in 1..=frequency_bound {}",
                self.alphabet, self.frequency_bound
            )));
        }
        Ok(())
    }

    pub fn budgets(&self) -> crate::container::Budgets {
        crate::container::Budgets {
            max_symbols: self.max_symbols,
            max_bytes: self.max_bytes,
            max_alphabet: self.max_alphabet,
        }
    }
}

/// Configuration failures.
#[derive(Debug)]
pub enum ConfigError {
    Io {
        path: PathBuf,
        message: String,
    },
    Parse {
        path: PathBuf,
        message: String,
    },
    BadEnv {
        name: String,
        value: String,
        message: String,
    },
    InvalidValue(String),
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Io { path, message } => {
                write!(f, "cannot read config {}: {message}", path.display())
            }
            ConfigError::Parse { path, message } => {
                write!(f, "invalid JSON in config {}: {message}", path.display())
            }
            ConfigError::BadEnv {
                name,
                value,
                message,
            } => {
                write!(f, "bad env {name}={value:?}: {message}")
            }
            ConfigError::InvalidValue(m) => write!(f, "invalid config: {m}"),
        }
    }
}

impl std::error::Error for ConfigError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_validate() {
        Config::default().validate().unwrap();
    }

    #[test]
    fn rejects_alphabet_above_bound() {
        let cfg = Config {
            frequency_bound: 8,
            alphabet: 256,
            ..Config::default()
        };
        assert!(cfg.validate().is_err());
    }
}
