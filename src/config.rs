//! 进程配置：全部来自环境变量，默认值适合本地运行。
//!
//! - `CNF_HTTP_BIND`：监听地址（默认 `127.0.0.1:8080`，默认只绑回环）。
//! - `CNF_MAX_DECISIONS`：每个请求的决策预算（默认 100 000；0 表示完全不允许决策，
//!   仅靠单位传播求解；-1 表示不限）。
//! - `CNF_TIME_LIMIT_MS`：每个请求的墙钟预算毫秒（默认 1 000；-1 表示不限）。
//! - `CNF_LOG_FORMAT`：`text`（默认）或 `json`。

use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    pub max_decisions: Option<u64>,
    pub time_limit: Option<Duration>,
    pub json_logs: bool,
}

#[derive(Debug)]
pub struct ConfigError {
    pub key: &'static str,
    pub value: String,
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "invalid config for {}: {:?}", self.key, self.value)
    }
}

impl std::error::Error for ConfigError {}

fn parse_limit(key: &'static str, raw: &str) -> Result<Option<u64>, ConfigError> {
    match raw.parse::<i64>() {
        Ok(-1) => Ok(None),
        Ok(n) if n >= 0 => Ok(Some(n as u64)),
        _ => Err(ConfigError {
            key,
            value: raw.to_string(),
        }),
    }
}

impl Config {
    pub fn from_env() -> Result<Self, ConfigError> {
        let bind = std::env::var("CNF_HTTP_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_string());

        let max_decisions = match std::env::var("CNF_MAX_DECISIONS") {
            Ok(raw) => parse_limit("CNF_MAX_DECISIONS", raw.trim())?,
            Err(_) => Some(100_000),
        };
        let time_limit = match std::env::var("CNF_TIME_LIMIT_MS") {
            Ok(raw) => parse_limit("CNF_TIME_LIMIT_MS", raw.trim())?.map(Duration::from_millis),
            Err(_) => Some(Duration::from_millis(1_000)),
        };
        let json_logs = std::env::var("CNF_LOG_FORMAT")
            .map(|v| v.trim().eq_ignore_ascii_case("json"))
            .unwrap_or(false);
        Ok(Config {
            bind,
            max_decisions,
            time_limit,
            json_logs,
        })
    }
}
