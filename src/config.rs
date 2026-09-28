//! Process configuration.
//!
//! Precedence (low→high): compiled defaults → optional JSON file (`MUS_CONFIG` or
//! `config/default.json`) → `MUS_*` environment variables.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// Bind address for the HTTP server.
    pub bind: String,
    pub log_level: String,
    /// `builtin` or `external`; external needs `external_binary`.
    pub solver_kind: String,
    pub external_binary: Option<String>,
    #[serde(default)]
    pub external_args: Vec<String>,
    /// Per-request cap clients are allowed to set for the solver-call budget.
    pub max_budget: u64,
    pub default_budget: u64,
    /// At most this many retained jobs are held in memory.
    pub max_jobs: usize,
    /// Log full input clauses? When false only ids/counts are logged (privacy).
    pub log_formulas: bool,
    /// Verify each report with a second independent solver on the API boundary.
    pub independent_verification: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            bind: "127.0.0.1:8080".to_string(),
            log_level: "info".to_string(),
            solver_kind: "builtin".to_string(),
            external_binary: None,
            external_args: Vec::new(),
            max_budget: 100_000,
            default_budget: 0,
            max_jobs: 256,
            log_formulas: false,
            independent_verification: true,
        }
    }
}

impl Config {
    /// Load defaults, overlay the JSON file if present, then overlay env vars.
    /// Missing config file is fine; malformed JSON is reported as an error.
    pub fn load() -> Result<Config, String> {
        let mut cfg = Config::default();

        let path = std::env::var("MUS_CONFIG")
            .unwrap_or_else(|_| "config/default.json".to_string());
        if let Ok(text) = std::fs::read_to_string(&path) {
            let file: HashMap<String, serde_json::Value> =
                serde_json::from_str(&text).map_err(|e| format!("config {path}: {e}"))?;
            apply_json(&mut cfg, &file);
        }

        if let Ok(v) = std::env::var("MUS_BIND") {
            cfg.bind = v;
        }
        if let Ok(v) = std::env::var("MUS_LOG_LEVEL") {
            cfg.log_level = v;
        }
        if let Ok(v) = std::env::var("MUS_SOLVER") {
            cfg.solver_kind = v;
        }
        if let Ok(v) = std::env::var("MUS_EXTERNAL_BINARY") {
            cfg.external_binary = Some(v);
        }
        if let Ok(v) = std::env::var("MUS_MAX_BUDGET") {
            cfg.max_budget = v.parse().map_err(|_| format!("MUS_MAX_BUDGET={v:?}"))?;
        }
        if let Ok(v) = std::env::var("MUS_LOG_FORMULAS") {
            cfg.log_formulas = matches!(v.as_str(), "1" | "true" | "yes");
        }
        if let Ok(v) = std::env::var("MUS_INDEPENDENT_VERIFICATION") {
            cfg.independent_verification = matches!(v.as_str(), "1" | "true" | "yes");
        }
        Ok(cfg)
    }
}

fn apply_json(cfg: &mut Config, map: &HashMap<String, serde_json::Value>) {
    if let Some(v) = map.get("bind").and_then(|v| v.as_str()) {
        cfg.bind = v.to_string();
    }
    if let Some(v) = map.get("log_level").and_then(|v| v.as_str()) {
        cfg.log_level = v.to_string();
    }
    if let Some(v) = map.get("solver_kind").and_then(|v| v.as_str()) {
        cfg.solver_kind = v.to_string();
    }
    if let Some(v) = map.get("external_binary").and_then(|v| v.as_str()) {
        cfg.external_binary = Some(v.to_string());
    }
    if let Some(v) = map.get("external_args").and_then(|v| v.as_array()) {
        cfg.external_args = v.iter().filter_map(|x| x.as_str().map(str::to_string)).collect();
    }
    if let Some(v) = map.get("max_budget").and_then(|v| v.as_u64()) {
        cfg.max_budget = v;
    }
    if let Some(v) = map.get("default_budget").and_then(|v| v.as_u64()) {
        cfg.default_budget = v;
    }
    if let Some(v) = map.get("max_jobs").and_then(|v| v.as_u64()) {
        cfg.max_jobs = v as usize;
    }
    if let Some(v) = map.get("log_formulas").and_then(|v| v.as_bool()) {
        cfg.log_formulas = v;
    }
    if let Some(v) = map.get("independent_verification").and_then(|v| v.as_bool()) {
        cfg.independent_verification = v;
    }
}
