//! File-based configuration overlay.
//!
//! Config files are plain JSON (deserialised with the already-pinned
//! serde_json, so no new dependency is introduced). Every key is optional: a
//! file is merged onto [`AnalyzerConfig`] / [`VerifyConfig`] defaults, and
//! explicit command-line flags take precedence over file values.
use ia_solver::AnalyzerConfig;
use ia_verify::VerifyConfig;
use serde_json::Value;
use std::path::Path;

#[derive(Debug)]
pub struct ConfigError {
    pub message: String,
}

fn load(path: &Path) -> Result<Value, ConfigError> {
    let text = std::fs::read_to_string(path).map_err(|e| ConfigError {
        message: format!("cannot read config {}: {e}", path.display()),
    })?;
    serde_json::from_str::<Value>(&text).map_err(|e| ConfigError {
        message: format!("invalid JSON in config {}: {e}", path.display()),
    })
}

fn bool_field(v: &Value, key: &str) -> Result<Option<bool>, ConfigError> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(b)) => Ok(Some(*b)),
        Some(_) => Err(ConfigError {
            message: format!("config key `{key}` must be a boolean"),
        }),
    }
}

fn usize_field(v: &Value, key: &str) -> Result<Option<usize>, ConfigError> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(n) => n
            .as_u64()
            .map(|n| Some(n as usize))
            .ok_or(ConfigError {
                message: format!("config key `{key}` must be a non-negative integer"),
            }),
    }
}

fn u64_field(v: &Value, key: &str) -> Result<Option<u64>, ConfigError> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(n) => n.as_u64().map(Some).ok_or(ConfigError {
            message: format!("config key `{key}` must be a non-negative integer"),
        }),
    }
}

/// Overlay analyzer config. Flags passed explicitly on the command line
/// (`cli_overrides`) win over values present in the file.
pub fn analyzer_config(
    path: Option<&Path>,
    cli_narrowing: Option<bool>,
    cli_plain: Option<bool>,
    cli_trace: Option<usize>,
) -> Result<AnalyzerConfig, ConfigError> {
    let mut cfg = AnalyzerConfig::default();
    if let Some(p) = path {
        let v = load(p)?;
        if let Some(b) = bool_field(&v, "narrowing")? {
            cfg.narrowing = b;
        }
        if let Some(b) = bool_field(&v, "plain_fixpoint")? {
            cfg.plain_fixpoint = b;
        }
        if let Some(n) = usize_field(&v, "max_trace_events")? {
            cfg.max_trace_events = n;
        }
    }
    if let Some(b) = cli_narrowing {
        cfg.narrowing = b;
    }
    if let Some(b) = cli_plain {
        cfg.plain_fixpoint = b;
    }
    if let Some(n) = cli_trace {
        cfg.max_trace_events = n;
    }
    Ok(cfg)
}

pub fn verify_config(
    path: Option<&Path>,
    cli_cap: Option<u64>,
    cli_steps: Option<u64>,
    cli_narrowing: Option<bool>,
) -> Result<VerifyConfig, ConfigError> {
    let mut cfg = VerifyConfig::default();
    if let Some(p) = path {
        let v = load(p)?;
        if let Some(n) = u64_field(&v, "enumeration_cap")? {
            cfg.enumeration_cap = n;
        }
        if let Some(n) = u64_field(&v, "step_limit")? {
            cfg.step_limit = n;
        }
        if let Some(b) = bool_field(&v, "narrowing")? {
            cfg.narrowing = b;
        }
    }
    if let Some(n) = cli_cap {
        cfg.enumeration_cap = n;
    }
    if let Some(n) = cli_steps {
        cfg.step_limit = n;
    }
    if let Some(b) = cli_narrowing {
        cfg.narrowing = b;
    }
    Ok(cfg)
}
