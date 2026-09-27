//! Configuration layer: typed config loaded from TOML with environment overrides.
//!
//! The same limits are reported back in every analysis result, so callers can
//! see exactly which exploration budget and loop-unrolling bound applied.

use serde::{Deserialize, Serialize};

/// Defaults are deliberately small: the language is finite-width and an
/// exhaustive small-domain cross-check is expected to dominate cost.
pub const DEFAULT_MAX_PATHS: u32 = 256;
pub const DEFAULT_LOOP_UNROLL: u32 = 16;
pub const DEFAULT_SOLVER_TIMEOUT_MS: u32 = 2_000;
pub const DEFAULT_BIND: &str = "127.0.0.1:8080";
pub const DEFAULT_LOG_LEVEL: &str = "info";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct EngineConfig {
    /// Maximum number of feasible path branches the engine may explore.
    pub max_paths: u32,
    /// Maximum number of loop iterations symbolically unrolled per loop.
    pub loop_unroll: u32,
    /// Per satisfiability check timeout, in milliseconds.
    pub solver_timeout_ms: u32,
}

impl Default for EngineConfig {
    fn default() -> Self {
        EngineConfig {
            max_paths: DEFAULT_MAX_PATHS,
            loop_unroll: DEFAULT_LOOP_UNROLL,
            solver_timeout_ms: DEFAULT_SOLVER_TIMEOUT_MS,
        }
    }
}

impl EngineConfig {
    pub fn validate(&self) -> Result<(), String> {
        if self.max_paths == 0 {
            return Err("max_paths must be >= 1".into());
        }
        if self.loop_unroll == 0 {
            return Err("loop_unroll must be >= 1".into());
        }
        if self.solver_timeout_ms == 0 {
            return Err("solver_timeout_ms must be >= 1".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ServerConfig {
    pub bind: String,
    pub log_level: String,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            bind: DEFAULT_BIND.into(),
            log_level: DEFAULT_LOG_LEVEL.into(),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConfigFile {
    #[serde(default)]
    pub engine: EngineConfig,
    #[serde(default)]
    pub server: ServerConfig,
}

impl ConfigFile {
    pub fn from_toml(text: &str) -> Result<Self, String> {
        let cfg: ConfigFile = toml::from_str(text).map_err(|e| format!("config parse error: {e}"))?;
        cfg.validate()?;
        Ok(cfg)
    }

    pub fn validate(&self) -> Result<(), String> {
        self.engine.validate()?;
        if self.server.bind.is_empty() {
            return Err("server.bind must not be empty".into());
        }
        Ok(())
    }

    /// Apply environment overrides (used for the local synthetic deployment).
    pub fn apply_env(&mut self) -> Result<(), String> {
        if let Ok(v) = std::env::var("SYMEX_BIND") {
            self.server.bind = v;
        }
        if let Ok(v) = std::env::var("SYMEX_LOG_LEVEL") {
            self.server.log_level = v;
        }
        if let Ok(v) = std::env::var("SYMEX_MAX_PATHS") {
            self.engine.max_paths = v.parse().map_err(|_| "SYMEX_MAX_PATHS must be a u32")?;
        }
        if let Ok(v) = std::env::var("SYMEX_LOOP_UNROLL") {
            self.engine.loop_unroll = v.parse().map_err(|_| "SYMEX_LOOP_UNROLL must be a u32")?;
        }
        if let Ok(v) = std::env::var("SYMEX_SOLVER_TIMEOUT_MS") {
            self.engine.solver_timeout_ms =
                v.parse().map_err(|_| "SYMEX_SOLVER_TIMEOUT_MS must be a u32")?;
        }
        self.validate()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_load_and_validate() {
        let cfg = ConfigFile::from_toml("").unwrap();
        assert_eq!(cfg.engine.max_paths, DEFAULT_MAX_PATHS);
        assert!(cfg.validate().is_ok());
    }

    #[test]
    fn rejects_zero_budget() {
        let toml = "engine.max_paths = 0\n";
        assert!(ConfigFile::from_toml(toml).is_err());
    }

    #[test]
    fn parses_partial_toml() {
        let cfg = ConfigFile::from_toml("[engine]\nloop_unroll = 5\n").unwrap();
        assert_eq!(cfg.engine.loop_unroll, 5);
        assert_eq!(cfg.engine.max_paths, DEFAULT_MAX_PATHS);
    }
}
