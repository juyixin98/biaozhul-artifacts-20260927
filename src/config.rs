//! Server configuration with an explicit precedence chain:
//!
//! 1. fields in the TOML config file (if given),
//! 2. environment variables (`BDD_BIND_ADDR`, `BDD_MAX_TRUTH_TABLE_ROWS`,
//!    `BDD_LOG`),
//! 3. built-in defaults.
//!
//! No production credentials exist anywhere in the project; the config only
//! describes local serving limits.

use std::path::Path;

use serde::Deserialize;

/// File representation (all fields optional so env/defaults can fill gaps).
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq)]
pub struct ConfigFile {
    pub bind_addr: Option<String>,
    pub max_truth_table_rows: Option<u64>,
    pub log_level: Option<String>,
}

/// Fully-resolved runtime configuration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    pub bind_addr: String,
    pub max_truth_table_rows: u64,
    pub log_level: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            bind_addr: "127.0.0.1:8080".into(),
            max_truth_table_rows: 1 << 16,
            log_level: "info".into(),
        }
    }
}

impl Config {
    /// Resolve configuration, optionally starting from a TOML file.
    pub fn load(path: Option<&Path>) -> Result<Config, ConfigError> {
        let file = match path {
            Some(p) => {
                let text = std::fs::read_to_string(p).map_err(|e| ConfigError {
                    message: format!("cannot read config {}: {e}", p.display()),
                })?;
                toml_parse(&text)?
            }
            None => ConfigFile::default(),
        };
        Self::from_file(file)
    }

    /// Resolve from a parsed file plus environment overrides.
    pub fn from_file(file: ConfigFile) -> Result<Config, ConfigError> {
        let d = Config::default();

        let bind_addr = std::env::var("BDD_BIND_ADDR")
            .ok()
            .or(file.bind_addr)
            .unwrap_or(d.bind_addr);
        let max_truth_table_rows = match std::env::var("BDD_MAX_TRUTH_TABLE_ROWS").ok() {
            Some(v) => v.parse::<u64>().map_err(|e| ConfigError {
                message: format!("BDD_MAX_TRUTH_TABLE_ROWS is not a u64: {e}"),
            })?,
            None => file.max_truth_table_rows.unwrap_or(d.max_truth_table_rows),
        };
        if max_truth_table_rows == 0 {
            return Err(ConfigError {
                message: "max_truth_table_rows must be at least 1".into(),
            });
        }
        let log_level = std::env::var("BDD_LOG")
            .ok()
            .or(file.log_level)
            .unwrap_or(d.log_level);

        Ok(Config {
            bind_addr,
            max_truth_table_rows,
            log_level,
        })
    }
}

/// Tiny hand-rolled TOML subset parser for the flat config (avoids a
/// toml-crate dependency for four keys). Supports `key = "string"` and
/// `key = 123` with `#` comments and blank lines.
fn toml_parse(text: &str) -> Result<ConfigFile, ConfigError> {
    let mut cfg = ConfigFile::default();
    for (lineno, raw_line) in text.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (key, value) = line.split_once('=').ok_or_else(|| ConfigError {
            message: format!("line {}: expected `key = value`", lineno + 1),
        })?;
        let key = key.trim();
        let value = value.trim().trim_matches('"');
        match key {
            "bind_addr" => cfg.bind_addr = Some(value.to_string()),
            "log_level" => cfg.log_level = Some(value.to_string()),
            "max_truth_table_rows" => {
                cfg.max_truth_table_rows = Some(value.parse::<u64>().map_err(|e| ConfigError {
                    message: format!("line {}: {e}", lineno + 1),
                })?);
            }
            other => {
                return Err(ConfigError {
                    message: format!("line {}: unknown config key {other:?}", lineno + 1),
                })
            }
        }
    }
    Ok(cfg)
}

/// Configuration error (no secrets involved).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError {
    pub message: String,
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "config error: {}", self.message)
    }
}
impl std::error::Error for ConfigError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_safe() {
        let c = Config::from_file(ConfigFile::default()).unwrap();
        assert_eq!(c.bind_addr, "127.0.0.1:8080");
        assert!(c.max_truth_table_rows >= 1);
    }

    #[test]
    fn parses_flat_toml_subset() {
        let text = r#"
            # local dev
            bind_addr = "0.0.0.0:9000"
            max_truth_table_rows = 256
        "#;
        let f = toml_parse(text).unwrap();
        assert_eq!(f.bind_addr.as_deref(), Some("0.0.0.0:9000"));
        assert_eq!(f.max_truth_table_rows, Some(256));
    }

    #[test]
    fn rejects_zero_limit() {
        let err = Config::from_file(ConfigFile {
            max_truth_table_rows: Some(0),
            ..Default::default()
        })
        .unwrap_err();
        assert!(err.message.contains("at least 1"));
    }
}
