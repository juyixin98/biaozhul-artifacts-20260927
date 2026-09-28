//! File and environment configuration for the service.
//!
//! Configuration precedence: explicit CLI flags > environment variables > config file
//! (`server.toml`) > built-in defaults. Nothing here requires any external account.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct AppConfig {
    pub server: ServerConfig,
    pub solver: SolverConfig,
    pub engine: EngineLimitsConfig,
    pub verify: VerifyConfig,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ServerConfig {
    pub bind: String,
    pub port: u16,
    /// Maximum accepted request body size in bytes.
    pub max_body_bytes: usize,
    pub log_format: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SolverConfig {
    pub bin: String,
    pub timeout_ms: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct EngineLimitsConfig {
    pub max_paths: usize,
    pub max_loop_unroll: u32,
    pub enforce_domains: bool,
    pub record_limit: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct VerifyConfig {
    pub replay_step_limit: u64,
    /// Default cap on exhaustive-oracle assignments (0 disables the oracle).
    pub oracle_cap: u64,
}

impl Default for AppConfig {
    fn default() -> Self {
        AppConfig {
            server: ServerConfig {
                bind: "127.0.0.1".to_string(),
                port: 8080,
                max_body_bytes: 1_048_576,
                log_format: "text".to_string(),
            },
            solver: SolverConfig {
                bin: "z3".to_string(),
                timeout_ms: 5_000,
            },
            engine: EngineLimitsConfig {
                max_paths: 256,
                max_loop_unroll: 64,
                enforce_domains: true,
                record_limit: 500,
            },
            verify: VerifyConfig {
                replay_step_limit: 200_000,
                oracle_cap: 4096,
            },
        }
    }
}

impl AppConfig {
    /// Load defaults, overlay a TOML file if present, then environment overrides.
    pub fn load(path: Option<&str>) -> Result<AppConfig, ConfigError> {
        let mut cfg = AppConfig::default();
        if let Some(p) = path {
            let txt = std::fs::read_to_string(p).map_err(|e| ConfigError::Read {
                path: p.to_string(),
                source: e,
            })?;
            cfg = toml::from_str(&txt).map_err(|e| ConfigError::Parse {
                path: p.to_string(),
                source: e,
            })?;
        }
        cfg.apply_env();
        Ok(cfg)
    }

    fn apply_env(&mut self) {
        if let Ok(v) = std::env::var("SE_BIND") {
            self.server.bind = v;
        }
        if let Ok(v) = std::env::var("SE_PORT") {
            if let Ok(p) = v.parse() {
                self.server.port = p;
            }
        }
        if let Ok(v) = std::env::var("SE_Z3_BIN") {
            self.solver.bin = v;
        }
        if let Ok(v) = std::env::var("SE_Z3_TIMEOUT_MS") {
            if let Ok(t) = v.parse() {
                self.solver.timeout_ms = t;
            }
        }
        if let Ok(v) = std::env::var("SE_MAX_PATHS") {
            if let Ok(n) = v.parse() {
                self.engine.max_paths = n;
            }
        }
        if let Ok(v) = std::env::var("SE_MAX_LOOP_UNROLL") {
            if let Ok(n) = v.parse() {
                self.engine.max_loop_unroll = n;
            }
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("cannot read config {path}: {source}")]
    Read {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("cannot parse config {path}: {source}")]
    Parse {
        path: String,
        #[source]
        source: toml::de::Error,
    },
}
