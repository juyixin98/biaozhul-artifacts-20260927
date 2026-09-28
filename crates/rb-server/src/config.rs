//! 进程配置：全部来自环境变量，带安全默认值（127.0.0.1 回环、本地数据目录）。

use std::path::PathBuf;

/// 服务配置。
#[derive(Debug, Clone)]
pub struct Config {
    /// 监听地址。
    pub bind: String,
    /// 集合文件目录。
    pub data_dir: PathBuf,
    /// tracing 过滤指令。
    pub log_level: String,
}

impl Config {
    pub fn from_env() -> Self {
        Config {
            bind: std::env::var("RB_BIND").unwrap_or_else(|_| "127.0.0.1:8080".to_string()),
            data_dir: std::env::var("RB_DATA_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from("./data")),
            log_level: std::env::var("RB_LOG").unwrap_or_else(|_| "info".to_string()),
        }
    }
}
