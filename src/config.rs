//! Runtime configuration: optional TOML file plus environment overrides.
//!
//! Precedence (highest first): environment variables, TOML file, defaults.
//! Environment variables use the `HCOMP_` prefix, e.g.
//! `HCOMP_BIND_ADDR=127.0.0.1:9000`.

use crate::error::{Error, ErrorKind, Result};
use serde::Deserialize;

/// Default split threshold: independently coded blocks are at most this large.
pub const DEFAULT_MAX_BLOCK_LEN: u64 = 1 << 20; // 1 MiB
/// Default ceiling on accepted request bodies.
pub const DEFAULT_MAX_BODY_LEN: usize = 16 << 20; // 16 MiB
/// Default listen address.
pub const DEFAULT_BIND_ADDR: &str = "127.0.0.1:8787";
/// Default directory used for object storage.
pub const DEFAULT_STORE_DIR: &str = "./data";

/// Fully resolved configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Directory holding `.hcmp` object files.
    pub store_dir: String,
    /// Maximum encoded block size in bytes.
    pub max_block_len: u64,
    /// Maximum accepted HTTP body size in bytes.
    pub max_body_len: usize,
    /// Socket address the HTTP server binds to.
    pub bind_addr: String,
    /// `plain` or pretty debug formatting for logs.
    pub log_format: String,
}

/// On-disk TOML shape (all fields optional so partial files are allowed).
#[derive(Debug, Default, Deserialize)]
struct TomlConfig {
    store_dir: Option<String>,
    max_block_len: Option<u64>,
    max_body_len: Option<usize>,
    bind_addr: Option<String>,
    log_format: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            store_dir: DEFAULT_STORE_DIR.to_string(),
            max_block_len: DEFAULT_MAX_BLOCK_LEN,
            max_body_len: DEFAULT_MAX_BODY_LEN,
            bind_addr: DEFAULT_BIND_ADDR.to_string(),
            log_format: "plain".to_string(),
        }
    }
}

impl Config {
    /// Build configuration from an optional TOML file path, then apply
    /// environment overrides.
    pub fn load(path: Option<&str>) -> Result<Self> {
        let mut cfg = Config::default();
        if let Some(p) = path {
            let text = std::fs::read_to_string(p)
                .map_err(|e| Error::new(ErrorKind::Io, format!("reading config file {p}: {e}")))?;
            let toml_cfg: TomlConfig = toml::from_str(&text)
                .map_err(|e| Error::new(ErrorKind::Io, format!("parsing config file {p}: {e}")))?;
            if let Some(v) = toml_cfg.store_dir {
                cfg.store_dir = v;
            }
            if let Some(v) = toml_cfg.max_block_len {
                cfg.max_block_len = v;
            }
            if let Some(v) = toml_cfg.max_body_len {
                cfg.max_body_len = v;
            }
            if let Some(v) = toml_cfg.bind_addr {
                cfg.bind_addr = v;
            }
            if let Some(v) = toml_cfg.log_format {
                cfg.log_format = v;
            }
        }

        if let Ok(v) = std::env::var("HCOMP_STORE_DIR") {
            cfg.store_dir = v;
        }
        if let Ok(v) = std::env::var("HCOMP_MAX_BLOCK_LEN") {
            cfg.max_block_len = parse_env("HCOMP_MAX_BLOCK_LEN", &v)?;
        }
        if let Ok(v) = std::env::var("HCOMP_MAX_BODY_LEN") {
            cfg.max_body_len = parse_env("HCOMP_MAX_BODY_LEN", &v)?;
        }
        if let Ok(v) = std::env::var("HCOMP_BIND_ADDR") {
            cfg.bind_addr = v;
        }
        if let Ok(v) = std::env::var("HCOMP_LOG_FORMAT") {
            cfg.log_format = v;
        }

        cfg.validate()?;
        Ok(cfg)
    }

    /// Reject nonsensical values before the server starts.
    pub fn validate(&self) -> Result<()> {
        if self.max_block_len == 0 {
            return Err(Error::new(
                ErrorKind::Io,
                "max_block_len must be greater than zero",
            ));
        }
        if self.max_body_len == 0 {
            return Err(Error::new(
                ErrorKind::Io,
                "max_body_len must be greater than zero",
            ));
        }
        if (self.max_block_len as usize) > self.max_body_len {
            return Err(Error::new(
                ErrorKind::Io,
                format!(
                    "max_block_len ({}) may not exceed max_body_len ({})",
                    self.max_block_len, self.max_body_len
                ),
            ));
        }
        if self.bind_addr.is_empty() {
            return Err(Error::new(ErrorKind::Io, "bind_addr must be set"));
        }
        match self.log_format.as_str() {
            "plain" | "json" | "debug" => {}
            other => {
                return Err(Error::new(
                    ErrorKind::Io,
                    format!("log_format '{other}' must be one of plain, json, debug"),
                ))
            }
        }
        Ok(())
    }
}

fn parse_env<T>(name: &str, raw: &str) -> Result<T>
where
    T: std::str::FromStr,
{
    raw.parse::<T>().map_err(|_| {
        Error::new(
            ErrorKind::Io,
            format!("environment variable {name}={raw:?} is not a valid number"),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_valid() {
        Config::default().validate().unwrap();
    }

    #[test]
    fn parses_toml_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("hcomp.toml");
        std::fs::write(
            &path,
            "store_dir = \"/var/tmp/hcomp\"\nmax_block_len = 4096\nbind_addr = \"0.0.0.0:1\"\n",
        )
        .unwrap();
        let cfg = Config::load(Some(path.to_str().unwrap())).unwrap();
        assert_eq!(cfg.store_dir, "/var/tmp/hcomp");
        assert_eq!(cfg.max_block_len, 4096);
        assert_eq!(cfg.bind_addr, "0.0.0.0:1");
    }

    #[test]
    fn rejects_inconsistent_limits() {
        let cfg = Config {
            max_block_len: 100,
            max_body_len: 10,
            ..Config::default()
        };
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn rejects_bad_log_format() {
        let cfg = Config {
            log_format: "xml".to_string(),
            ..Config::default()
        };
        assert!(cfg.validate().is_err());
    }
}
