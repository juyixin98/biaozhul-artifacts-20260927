//! `hbs-config`: typed configuration with a small dependency-free file
//! format and `HBS_*` environment overrides.
//!
//! The file format is a deliberately tiny, explicitly supported subset:
//!
//! ```text
//! # a comment
//! data_dir = "./data"
//! bind_addr = "127.0.0.1:8080"
//! max_request_bytes = 1048576
//! sync_writes = true
//! log_level = "info"
//! ```
//!
//! Environment variables named `HBS_<FIELD>` (e.g. `HBS_BIND_ADDR`) override
//! file values. Unknown keys and malformed values produce a specific
//! [`ConfigError`] rather than being silently ignored.

use std::path::Path;

/// Server and storage configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Directory holding `.hbs` set files.
    pub data_dir: String,
    /// Socket address the HTTP server binds to.
    pub bind_addr: String,
    /// Largest accepted JSON request body, in bytes.
    pub max_request_bytes: usize,
    /// Whether writes are fsynced before success is reported.
    pub sync_writes: bool,
    /// Log filter (`error|warn|info|debug|trace`).
    pub log_level: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            data_dir: "./data".to_string(),
            bind_addr: "127.0.0.1:8080".to_string(),
            max_request_bytes: 1 << 20,
            sync_writes: true,
            log_level: "info".to_string(),
        }
    }
}

/// Configuration failures.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigError {
    /// The config file could not be read.
    Io(String),
    /// Syntax error at a 1-based line.
    Parse {
        /// Line number.
        line: usize,
        /// What was wrong.
        message: String,
    },
    /// Unknown configuration key.
    UnknownKey(String),
    /// Value could not be parsed into the field's type.
    BadValue {
        /// Field name.
        key: String,
        /// Raw value.
        value: String,
    },
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Io(e) => write!(f, "cannot read config file: {e}"),
            ConfigError::Parse { line, message } => {
                write!(f, "config parse error on line {line}: {message}")
            }
            ConfigError::UnknownKey(k) => write!(f, "unknown config key: {k}"),
            ConfigError::BadValue { key, value } => {
                write!(f, "bad value for {key}: {value:?}")
            }
        }
    }
}

impl std::error::Error for ConfigError {}

impl Config {
    /// Load defaults, overlay the file (if it exists), then environment.
    pub fn load(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
        let mut cfg = Config::default();
        let p = path.as_ref();
        if p.exists() {
            let text = std::fs::read_to_string(p).map_err(|e| ConfigError::Io(e.to_string()))?;
            cfg.apply_file(&text)?;
        }
        cfg.apply_env()?;
        Ok(cfg)
    }

    /// Parse and apply the supported subset.
    pub fn apply_file(&mut self, text: &str) -> Result<(), ConfigError> {
        for (idx, raw_line) in text.lines().enumerate() {
            let line_no = idx + 1;
            let line = raw_line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (key, value) = line.split_once('=').ok_or_else(|| ConfigError::Parse {
                line: line_no,
                message: "expected key = value".to_string(),
            })?;
            let key = key.trim();
            let value = value.trim();
            if key.is_empty() {
                return Err(ConfigError::Parse {
                    line: line_no,
                    message: "empty key".to_string(),
                });
            }
            let value = parse_scalar(value).ok_or_else(|| ConfigError::Parse {
                line: line_no,
                message: format!(
                    "value must be a quoted string, true/false or an integer: {value}"
                ),
            })?;
            self.set(key, &value).map_err(|e| match e {
                ConfigError::BadValue { key, value } => ConfigError::BadValue { key, value },
                other => other,
            })?;
        }
        Ok(())
    }

    /// Overlay any present `HBS_*` variables. Unknown or bad env values are
    /// reported rather than ignored, so a typo cannot silently misconfigure a
    /// deployment.
    pub fn apply_env(&mut self) -> Result<(), ConfigError> {
        let keys = [
            "DATA_DIR",
            "BIND_ADDR",
            "MAX_REQUEST_BYTES",
            "SYNC_WRITES",
            "LOG_LEVEL",
        ];
        for suffix in keys {
            let var = format!("HBS_{suffix}");
            if let Ok(raw) = std::env::var(&var) {
                let field = suffix.to_lowercase();
                self.set(&field, &raw)?;
            }
        }
        Ok(())
    }

    fn set(&mut self, key: &str, value: &str) -> Result<(), ConfigError> {
        let bad = || ConfigError::BadValue {
            key: key.to_string(),
            value: value.to_string(),
        };
        match key {
            "data_dir" => self.data_dir = value.to_string(),
            "bind_addr" => self.bind_addr = value.to_string(),
            "max_request_bytes" => {
                self.max_request_bytes = value.parse::<usize>().map_err(|_| bad())?;
                if self.max_request_bytes == 0 {
                    return Err(bad());
                }
            }
            "sync_writes" => {
                self.sync_writes = match value {
                    "true" => true,
                    "false" => false,
                    _ => return Err(bad()),
                };
            }
            "log_level" => {
                if !["error", "warn", "info", "debug", "trace"].contains(&value) {
                    return Err(bad());
                }
                self.log_level = value.to_string();
            }
            other => return Err(ConfigError::UnknownKey(other.to_string())),
        }
        Ok(())
    }
}

fn parse_scalar(raw: &str) -> Option<String> {
    if let Some(rest) = raw.strip_prefix('"') {
        let inner = rest.strip_suffix('"')?;
        // Escaping is intentionally not supported; reject backslashes so a
        // value cannot be ambiguous.
        if inner.contains('\\') {
            return None;
        }
        Some(inner.to_string())
    } else if raw == "true"
        || raw == "false"
        || (!raw.is_empty() && raw.bytes().all(|b| b.is_ascii_digit()))
    {
        // Bare scalars: booleans or non-negative integers (strings must be
        // quoted, which keeps the grammar unambiguous).
        Some(raw.to_string())
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_and_file_overlay() {
        let mut c = Config::default();
        c.apply_file(
            "# comment\n\
             data_dir = \"/tmp/x\"\n\
             bind_addr = \"0.0.0.0:9000\"\n\
             max_request_bytes = 2048\n\
             sync_writes = false\n",
        )
        .unwrap();
        assert_eq!(c.data_dir, "/tmp/x");
        assert_eq!(c.bind_addr, "0.0.0.0:9000");
        assert_eq!(c.max_request_bytes, 2048);
        assert!(!c.sync_writes);
        assert_eq!(c.log_level, "info");
    }

    #[test]
    fn specific_errors() {
        let mut c = Config::default();
        let err = c.apply_file("nope = 1").unwrap_err();
        assert!(matches!(err, ConfigError::UnknownKey(k) if k == "nope"));

        let err = c.apply_file("max_request_bytes = true").unwrap_err();
        assert!(matches!(err, ConfigError::BadValue { .. }));

        let err = c.apply_file("garbage line").unwrap_err();
        assert!(matches!(err, ConfigError::Parse { line: 1, .. }));

        let err = c.apply_file("sync_writes = 5").unwrap_err();
        assert!(matches!(err, ConfigError::BadValue { .. }));

        let err = c.apply_file("log_level = \"verbose\"").unwrap_err();
        assert!(matches!(err, ConfigError::BadValue { .. }));
    }
}
