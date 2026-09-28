//! 运行配置：内置默认值，可用环境变量覆盖；无需外部账号与服务。
//!
//! 环境变量：
//! - `CNF_API_BIND`：监听地址（默认 `127.0.0.1:8080`）
//! - `CNF_TIME_LIMIT_MS`：单次求解墙钟上限毫秒（默认 10000）
//! - `CNF_MAX_PROPAGATIONS`：传播上限（默认 10_000_000）
//! - `CNF_MAX_DECISIONS`：决策上限（默认 1_000_000）

use std::time::Duration;

use crate::solver::budget::Budget;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub bind: String,
    pub budget: Budget,
}

impl Default for AppConfig {
    fn default() -> Self {
        AppConfig {
            bind: "127.0.0.1:8080".to_string(),
            budget: Budget::default(),
        }
    }
}

fn env_or<T: std::str::FromStr>(key: &str, default: T) -> T {
    std::env::var(key).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

impl AppConfig {
    pub fn from_env() -> Self {
        let def = AppConfig::default();
        AppConfig {
            bind: std::env::var("CNF_API_BIND").unwrap_or(def.bind),
            budget: Budget {
                time_limit: Some(Duration::from_millis(env_or(
                    "CNF_TIME_LIMIT_MS",
                    def.budget
                        .time_limit
                        .map(|d| d.as_millis() as u64)
                        .unwrap_or(10_000),
                ))),
                max_propagations: Some(env_or(
                    "CNF_MAX_PROPAGATIONS",
                    def.budget.max_propagations.unwrap_or(10_000_000),
                )),
                max_decisions: Some(env_or(
                    "CNF_MAX_DECISIONS",
                    def.budget.max_decisions.unwrap_or(1_000_000),
                )),
            },
        }
    }
}
