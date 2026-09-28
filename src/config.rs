//! Startup configuration, loaded from environment variables with defaults.
//!
//! | Variable | Default | Meaning |
//! |---|---|---|
//! | `FM_DATA_DIR` | `./data` | catalog + index file directory |
//! | `FM_BIND` | `127.0.0.1:8080` | listen address |
//! | `FM_MAX_TEXT_BYTES` | `4194304` (4 MiB) | max uploaded text size |
//! | `FM_DEFAULT_SAMPLE_INTERVAL` | `16` | SA sampling when request omits it |
//! | `FM_LOG_DIR` | (unset) | if set, JSONL request logs go there too |
//! | `RUST_LOG` | `fm_index_svc=debug,tower_http=info` | tracing filter |

use std::path::PathBuf;

use crate::error::{Error, Result};

#[derive(Debug, Clone)]
pub struct Config {
    pub data_dir: PathBuf,
    pub bind: String,
    pub max_text_bytes: usize,
    pub default_sample_interval: u32,
    pub log_dir: Option<PathBuf>,
    pub rust_log: String,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let cfg = Self {
            data_dir: PathBuf::from(env_or("FM_DATA_DIR", "data")),
            bind: env_or("FM_BIND", "127.0.0.1:8080"),
            max_text_bytes: env_parse("FM_MAX_TEXT_BYTES", 4 * 1024 * 1024)?,
            default_sample_interval: env_parse("FM_DEFAULT_SAMPLE_INTERVAL", 16)?,
            log_dir: std::env::var_os("FM_LOG_DIR").map(PathBuf::from),
            rust_log: env_or("RUST_LOG", "fm_index_svc=debug,tower_http=info"),
        };
        if cfg.max_text_bytes == 0 {
            return Err(Error::MissingField("FM_MAX_TEXT_BYTES must be > 0"));
        }
        Ok(cfg)
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn env_parse<T: std::str::FromStr>(key: &str, default: T) -> Result<T> {
    match std::env::var(key) {
        Ok(v) => v.parse().map_err(|_| Error::BadName {
            name: format!("{key}={v}"),
            reason: "environment variable has a non-numeric value",
        }),
        Err(_) => Ok(default),
    }
}
