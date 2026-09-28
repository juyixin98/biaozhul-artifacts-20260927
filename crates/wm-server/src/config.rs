//! Service configuration.
//!
//! Precedence (highest first): CLI flags > environment variables
//! (`WM_*`) > config TOML file > built-in defaults.

use std::path::PathBuf;

use serde::Deserialize;

/// Fully resolved service configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Directory holding the `<name>.wmi` index files.
    pub data_dir: PathBuf,
    /// Bind host.
    pub host: String,
    /// Bind port.
    pub port: u16,
    /// `tracing` filter directive.
    pub log_level: String,
    /// Config file the values were loaded from, if any.
    pub source: Option<PathBuf>,
}

#[derive(Debug, Default, Deserialize)]
struct FileConfig {
    data: Option<DataSection>,
    server: Option<ServerSection>,
    log: Option<LogSection>,
}

#[derive(Debug, Default, Deserialize)]
struct DataSection {
    dir: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
struct ServerSection {
    host: Option<String>,
    port: Option<u16>,
}

#[derive(Debug, Default, Deserialize)]
struct LogSection {
    level: Option<String>,
}

/// Errors while loading configuration.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("failed to read config file {}: {source}", path.display())]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("failed to parse config file {}: {source}", path.display())]
    Parse {
        path: PathBuf,
        source: toml::de::Error,
    },
}

impl Config {
    /// Load configuration from `file` (if present), layered with env
    /// variables and CLI overrides.
    pub fn load(
        file: Option<&std::path::Path>,
        cli_host: Option<String>,
        cli_port: Option<u16>,
        cli_data_dir: Option<PathBuf>,
        cli_log_level: Option<String>,
    ) -> Result<Self, ConfigError> {
        let mut cfg = FileConfig::default();
        let mut used_source = None;
        if let Some(path) = file {
            let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Read {
                path: path.to_path_buf(),
                source,
            })?;
            cfg = toml::from_str(&text).map_err(|source| ConfigError::Parse {
                path: path.to_path_buf(),
                source,
            })?;
            used_source = Some(path.to_path_buf());
        }

        let env = |key: &str| std::env::var(key).ok().filter(|s| !s.is_empty());

        let data_dir = cli_data_dir
            .or_else(|| env("WM_DATA_DIR").map(PathBuf::from))
            .or_else(|| cfg.data.and_then(|d| d.dir).map(PathBuf::from))
            .unwrap_or_else(|| PathBuf::from("./data"));

        let host = cli_host
            .or_else(|| env("WM_HOST"))
            .or_else(|| cfg.server.as_ref().and_then(|s| s.host.clone()))
            .unwrap_or_else(|| "127.0.0.1".to_string());

        let port = cli_port
            .or_else(|| env("WM_PORT").and_then(|p| p.parse().ok()))
            .or_else(|| cfg.server.as_ref().and_then(|s| s.port))
            .unwrap_or(8080);

        let log_level = cli_log_level
            .or_else(|| env("WM_LOG_LEVEL"))
            .or_else(|| cfg.log.and_then(|l| l.level))
            .unwrap_or_else(|| "info".to_string());

        Ok(Self {
            data_dir,
            host,
            port,
            log_level,
            source: used_source,
        })
    }
}
