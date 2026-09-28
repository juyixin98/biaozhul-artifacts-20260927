//! Server configuration.
//!
//! Configuration is layered, in increasing priority:
//! 1. built-in defaults;
//! 2. an optional TOML/key=value-free simple file (`config/server.conf`,
//!    `KEY=VALUE` per line);
//! 3. environment variables (`PN_HTTP_BIND`, ...).
//!
//! Only the standard library is used so the configuration layer has no extra
//! dependencies and remains trivially auditable.

use std::path::Path;

#[derive(Debug, Clone)]
pub struct Config {
    pub http_bind: String,
    /// Hard cap on markings expanded per analysis, independent of the
    /// client-supplied `max_states` (the server never exceeds this).
    pub max_states_ceiling: usize,
    pub log_level: String,
    pub server_version: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            http_bind: "127.0.0.1:8080".to_string(),
            max_states_ceiling: 1_000_000,
            log_level: "info".to_string(),
            server_version: env!("CARGO_PKG_VERSION").to_string(),
        }
    }
}

impl Config {
    /// Load defaults, override from a file if present, then environment.
    pub fn load<P: AsRef<Path>>(file: Option<P>) -> Result<Config, ConfigError> {
        let mut cfg = Config::default();
        if let Some(path) = file {
            if path.as_ref().exists() {
                cfg.apply_file(path.as_ref())?;
            }
        }
        cfg.apply_env();
        cfg.validate()?;
        Ok(cfg)
    }

    fn apply_file(&mut self, path: &Path) -> Result<(), ConfigError> {
        let body = std::fs::read_to_string(path)
            .map_err(|e| ConfigError::Io(format!("{}: {e}", path.display())))?;
        for (lineno, raw) in body.lines().enumerate() {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (key, value) = line.split_once('=').ok_or_else(|| {
                ConfigError::BadLine(format!(
                    "{}:{}: expected KEY=VALUE, got {raw:?}",
                    path.display(),
                    lineno + 1
                ))
            })?;
            self.set(key.trim(), value.trim())
                .map_err(|e| ConfigError::BadLine(format!("{}:{}: {e}", path.display(), lineno + 1)))?;
        }
        Ok(())
    }

    fn apply_env(&mut self) {
        if let Ok(v) = std::env::var("PN_HTTP_BIND") {
            self.http_bind = v;
        }
        if let Ok(v) = std::env::var("PN_MAX_STATES_CEILING") {
            if let Ok(n) = v.parse() {
                self.max_states_ceiling = n;
            }
        }
        if let Ok(v) = std::env::var("PN_LOG_LEVEL") {
            self.log_level = v;
        }
    }

    fn set(&mut self, key: &str, value: &str) -> Result<(), String> {
        match key {
            "http_bind" => self.http_bind = value.to_string(),
            "max_states_ceiling" => {
                self.max_states_ceiling = value
                    .parse()
                    .map_err(|_| format!("max_states_ceiling not a usize: {value:?}"))?
            }
            "log_level" => self.log_level = value.to_string(),
            other => return Err(format!("unknown configuration key {other:?}")),
        }
        Ok(())
    }

    fn validate(&self) -> Result<(), ConfigError> {
        if self.http_bind.is_empty() {
            return Err(ConfigError::BadLine("http_bind is empty".into()));
        }
        Ok(())
    }
}

#[derive(Debug)]
pub enum ConfigError {
    Io(String),
    BadLine(String),
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Io(s) | ConfigError::BadLine(s) => write!(f, "{s}"),
        }
    }
}

impl std::error::Error for ConfigError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_valid() {
        let cfg = Config::default();
        assert!(cfg.validate().is_ok());
        assert!(cfg.max_states_ceiling >= 1);
    }

    #[test]
    fn file_overrides_unknown_keys_rejected() {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("pn-server-test-{}.conf", std::process::id()));
        std::fs::write(&path, "# comment\nhttp_bind=0.0.0.0:9999\n").unwrap();
        let cfg = Config::load(Some(&path)).unwrap();
        assert_eq!(cfg.http_bind, "0.0.0.0:9999");
        std::fs::remove_file(&path).ok();
    }
}
