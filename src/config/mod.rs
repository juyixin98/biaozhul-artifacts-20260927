//! 配置层：TOML 文件（默认 `config/default.toml`，可被 `config/local.toml` 覆盖），
//! 再由 `PETRI_*` 环境变量覆盖单项配置。

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    #[serde(default = "default_host")]
    pub server_host: String,
    #[serde(default = "default_port")]
    pub server_port: u16,
    #[serde(default = "default_body_limit")]
    pub body_limit_bytes: usize,
    #[serde(default = "default_state_limit")]
    pub solver_state_limit: u64,
    #[serde(default = "default_coeff_bound")]
    pub invariant_coefficient_bound: i64,
    #[serde(default = "default_max_combinations")]
    pub invariant_max_combinations: usize,
    #[serde(default = "default_log_level")]
    pub log_level: String,
}

fn default_host() -> String {
    "127.0.0.1".into()
}
fn default_port() -> u16 {
    8080
}
fn default_body_limit() -> usize {
    1 << 20
}
fn default_state_limit() -> u64 {
    200_000
}
fn default_coeff_bound() -> i64 {
    4
}
fn default_max_combinations() -> usize {
    2_000_000
}
fn default_log_level() -> String {
    "info".into()
}

impl Default for Config {
    fn default() -> Self {
        Config {
            server_host: default_host(),
            server_port: default_port(),
            body_limit_bytes: default_body_limit(),
            solver_state_limit: default_state_limit(),
            invariant_coefficient_bound: default_coeff_bound(),
            invariant_max_combinations: default_max_combinations(),
            log_level: default_log_level(),
        }
    }
}

impl Config {
    /// 内置默认配置 -> `config/local.toml`（若存在）-> 环境变量。
    pub fn load() -> Result<Config, ConfigError> {
        let mut cfg = Config::default();

        let local = std::path::Path::new("config/local.toml");
        if local.exists() {
            let text = std::fs::read_to_string(local).map_err(|e| ConfigError::Io(e.to_string()))?;
            cfg = merge_toml(cfg, &text)?;
        } else {
            let builtin = include_str!("../../config/default.toml");
            cfg = merge_toml(cfg, builtin)?;
        }
        if let Ok(v) = std::env::var("PETRI_SERVER_HOST") {
            cfg.server_host = v;
        }
        if let Ok(v) = std::env::var("PETRI_SERVER_PORT") {
            cfg.server_port = v.parse().map_err(|_| ConfigError::BadEnv("PETRI_SERVER_PORT".into()))?;
        }
        if let Ok(v) = std::env::var("PETRI_BODY_LIMIT_BYTES") {
            cfg.body_limit_bytes = v.parse().map_err(|_| ConfigError::BadEnv("PETRI_BODY_LIMIT_BYTES".into()))?;
        }
        if let Ok(v) = std::env::var("PETRI_SOLVER_STATE_LIMIT") {
            cfg.solver_state_limit = v.parse().map_err(|_| ConfigError::BadEnv("PETRI_SOLVER_STATE_LIMIT".into()))?;
        }
        if let Ok(v) = std::env::var("PETRI_INVARIANT_COEFFICIENT_BOUND") {
            cfg.invariant_coefficient_bound = v.parse().map_err(|_| ConfigError::BadEnv("PETRI_INVARIANT_COEFFICIENT_BOUND".into()))?;
        }
        if let Ok(v) = std::env::var("PETRI_INVARIANT_MAX_COMBINATIONS") {
            cfg.invariant_max_combinations = v.parse().map_err(|_| ConfigError::BadEnv("PETRI_INVARIANT_MAX_COMBINATIONS".into()))?;
        }
        if let Ok(v) = std::env::var("PETRI_LOG_LEVEL") {
            cfg.log_level = v;
        }
        Ok(cfg)
    }

    pub fn bind_address(&self) -> String {
        format!("{}:{}", self.server_host, self.server_port)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("config I/O error: {0}")]
    Io(String),
    #[error("invalid TOML config: {0}")]
    Toml(String),
    #[error("invalid environment variable {0}")]
    BadEnv(String),
}

fn merge_toml(base: Config, text: &str) -> Result<Config, ConfigError> {
    // 反序列化为 Option 字段的影子结构，仅覆盖出现的键。
    #[derive(Deserialize, Default)]
    struct Overlay {
        server_host: Option<String>,
        server_port: Option<u16>,
        body_limit_bytes: Option<usize>,
        solver_state_limit: Option<u64>,
        invariant_coefficient_bound: Option<i64>,
        invariant_max_combinations: Option<usize>,
        log_level: Option<String>,
    }
    let overlay: Overlay = toml::from_str(text).map_err(|e| ConfigError::Toml(e.to_string()))?;
    Ok(Config {
        server_host: overlay.server_host.unwrap_or(base.server_host),
        server_port: overlay.server_port.unwrap_or(base.server_port),
        body_limit_bytes: overlay.body_limit_bytes.unwrap_or(base.body_limit_bytes),
        solver_state_limit: overlay.solver_state_limit.unwrap_or(base.solver_state_limit),
        invariant_coefficient_bound: overlay
            .invariant_coefficient_bound
            .unwrap_or(base.invariant_coefficient_bound),
        invariant_max_combinations: overlay
            .invariant_max_combinations
            .unwrap_or(base.invariant_max_combinations),
        log_level: overlay.log_level.unwrap_or(base.log_level),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_builtin_toml() {
        let cfg = Config::default();
        assert_eq!(cfg.server_host, "127.0.0.1");
        assert_eq!(cfg.server_port, 8080);
        assert_eq!(cfg.solver_state_limit, 200_000);
        assert_eq!(cfg.invariant_coefficient_bound, 4);
    }

    #[test]
    fn merge_only_overrides_present_keys() {
        let base = Config::default();
        let merged = merge_toml(base, "server_port = 9999\n").unwrap();
        assert_eq!(merged.server_port, 9999);
        // 未出现的键保持默认。
        assert_eq!(merged.server_host, "127.0.0.1");
        assert_eq!(merged.solver_state_limit, 200_000);
    }

    #[test]
    fn bad_toml_is_an_error_not_silently_ignored() {
        let base = Config::default();
        let err = merge_toml(base, "server_port = \"not-a-number\"\n");
        assert!(err.is_err(), "type mismatch must be reported, not swallowed");
    }

    #[test]
    fn bind_address_format() {
        let cfg = Config::default();
        assert_eq!(cfg.bind_address(), "127.0.0.1:8080");
    }
}
