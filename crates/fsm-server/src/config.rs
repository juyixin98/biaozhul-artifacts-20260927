//! Runtime configuration.
//!
//! Resolution order (highest priority first):
//! explicit CLI flags > environment variables (`FSM_*`) > TOML config file
//! > built-in defaults.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Config {
    pub host: String,
    pub port: u16,
    pub budget: BudgetConfig,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct BudgetConfig {
    pub max_states: u64,
    pub max_transitions: u64,
    pub max_initial_scan: u64,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            host: "127.0.0.1".to_string(),
            port: 8080,
            budget: BudgetConfig::default(),
        }
    }
}

impl Default for BudgetConfig {
    fn default() -> Self {
        BudgetConfig {
            max_states: 100_000,
            max_transitions: 1_000_000,
            max_initial_scan: 1_000_000,
        }
    }
}

impl Config {
    /// Load defaults, then overlay a TOML file (if present), then
    /// `FSM_HOST` / `FSM_PORT` / `FSM_MAX_STATES` / `FSM_MAX_TRANSITIONS` /
    /// `FSM_MAX_INITIAL_SCAN`.
    pub fn load(file_path: Option<&str>) -> Result<Config, String> {
        let mut config = Config::default();
        if let Some(path) = file_path {
            let text = std::fs::read_to_string(path)
                .map_err(|e| format!("cannot read config {path}: {e}"))?;
            config = toml::from_str(&text).map_err(|e| format!("invalid config {path}: {e}"))?;
        }
        if let Ok(v) = std::env::var("FSM_HOST") {
            config.host = v;
        }
        if let Ok(v) = std::env::var("FSM_PORT") {
            config.port = v
                .parse()
                .map_err(|_| format!("invalid FSM_PORT: {v}"))?;
        }
        if let Ok(v) = std::env::var("FSM_MAX_STATES") {
            config.budget.max_states = v
                .parse()
                .map_err(|_| format!("invalid FSM_MAX_STATES: {v}"))?;
        }
        if let Ok(v) = std::env::var("FSM_MAX_TRANSITIONS") {
            config.budget.max_transitions = v
                .parse()
                .map_err(|_| format!("invalid FSM_MAX_TRANSITIONS: {v}"))?;
        }
        if let Ok(v) = std::env::var("FSM_MAX_INITIAL_SCAN") {
            config.budget.max_initial_scan = v
                .parse()
                .map_err(|_| format!("invalid FSM_MAX_INITIAL_SCAN: {v}"))?;
        }
        Ok(config)
    }
}
