//! Configuration loaded from environment variables (12-factor style),
//! with explicit defaults (see README.md §Configuration).
//!
//! | variable            | default          | meaning                         |
//! |---------------------|------------------|---------------------------------|
//! | `HUFF_BIND`         | `127.0.0.1:8080` | serve listen address            |
//! | `HUFF_DATA_DIR`     | `./data`         | artifact store root             |
//! | `HUFF_BLOCK_SIZE`   | `65536`          | encoder block input size        |
//! | `HUFF_MAX_BODY`     | `16777216`       | max request body bytes (16 MiB) |
//! | `HUFF_LOG_DIR`      | unset            | if set, logs are mirrored there |

use std::env;
use std::path::PathBuf;

use huff_core::{MAX_BLOCK_SIZE, DEFAULT_BLOCK_SIZE};

/// Runtime configuration.
#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    pub data_dir: PathBuf,
    pub block_size: u32,
    pub max_body: usize,
    pub log_dir: Option<PathBuf>,
}

/// Configuration error.
#[derive(Debug, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "configuration error: {}", self.0)
    }
}

impl std::error::Error for ConfigError {}

fn env_or(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_string())
}

impl Config {
    /// Build configuration from the process environment.
    pub fn from_env() -> Result<Self, ConfigError> {
        let bind = env_or("HUFF_BIND", "127.0.0.1:8080");
        let data_dir = PathBuf::from(env_or("HUFF_DATA_DIR", "data"));
        let log_dir = env::var("HUFF_LOG_DIR").ok().map(PathBuf::from);

        let block_size = env_or("HUFF_BLOCK_SIZE", &DEFAULT_BLOCK_SIZE.to_string())
            .parse::<u32>()
            .map_err(|_| ConfigError("HUFF_BLOCK_SIZE must be a positive u32".to_string()))?;
        if block_size == 0 || block_size > MAX_BLOCK_SIZE {
            return Err(ConfigError(format!(
                "HUFF_BLOCK_SIZE must be in 1..={MAX_BLOCK_SIZE}"
            )));
        }

        let max_body = env_or("HUFF_MAX_BODY", "16777216")
            .parse::<usize>()
            .map_err(|_| ConfigError("HUFF_MAX_BODY must be a positive usize".to_string()))?;
        if max_body == 0 {
            return Err(ConfigError("HUFF_MAX_BODY must be > 0".to_string()));
        }

        Ok(Self { bind, data_dir, block_size, max_body, log_dir })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_known() {
        // These tests do not mutate the shared process env; they only assert
        // the documented default constants.
        assert_eq!(DEFAULT_BLOCK_SIZE, 65536);
        assert!(MAX_BLOCK_SIZE >= DEFAULT_BLOCK_SIZE);
    }

    #[test]
    fn block_size_bounds_are_documented() {
        assert!(MAX_BLOCK_SIZE == 1 << 20);
    }
}
