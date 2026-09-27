//! 启动配置：`config.toml` + 环境变量 + 命令行默认值（命令行优先）。
//!
//! 查找顺序（main 中实现）：`--config <path>` > `FM_INDEX_CONFIG` 环境变量
//! > 当前目录 `./config.toml`；找不到文件时使用内置默认值。

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::error::{FmError, Result};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub server: ServerConfig,
    pub storage: StorageConfig,
    pub index: IndexDefaults,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    /// HTTP 请求体上限（字节）。JSON/base64 会膨胀约 4/3，创建索引时按此拦截。
    pub max_body_bytes: u64,
    /// 单次 locate 返回位置数上限，超出报 resource_exhausted。
    pub max_locations: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StorageConfig {
    /// 索引根目录（每个索引一个子目录）。
    pub data_dir: PathBuf,
    /// 允许从服务本地文件系统导入文本的白名单目录；为空表示禁止文件导入。
    pub import_dirs: Vec<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexDefaults {
    /// 建索引接受的最大文本字节数。
    pub max_text_bytes: u64,
    pub rank_block: u32,
    pub sample_step: u32,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            server: ServerConfig {
                host: "127.0.0.1".to_string(),
                port: 8921,
                max_body_bytes: 24 * 1024 * 1024,
                max_locations: 1_000_000,
            },
            storage: StorageConfig {
                data_dir: PathBuf::from("./data/indexes"),
                import_dirs: vec![PathBuf::from("./examples")],
            },
            index: IndexDefaults {
                max_text_bytes: 16 * 1024 * 1024,
                rank_block: 256,
                sample_step: 16,
            },
        }
    }
}

impl Config {
    /// 从 TOML 文件加载；文件不存在返回 `Ok(None)`（调用方回退默认值）。
    pub fn load_file(path: &std::path::Path) -> Result<Option<Config>> {
        match std::fs::read_to_string(path) {
            Ok(content) => {
                let cfg: Config = toml::from_str(&content).map_err(|e| {
                    FmError::invalid_input(format!("配置文件 {path:?} 解析失败: {e}"))
                })?;
                cfg.validate()?;
                Ok(Some(cfg))
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(FmError::from(e)),
        }
    }

    /// 以默认值为底，叠加文件（若有），再叠加命令行覆盖项。
    pub fn assemble(
        path: Option<&std::path::Path>,
        host: Option<String>,
        port: Option<u16>,
        data_dir: Option<PathBuf>,
    ) -> Result<Config> {
        let mut cfg = match path {
            Some(p) => Config::load_file(p)?.unwrap_or_default(),
            None => Config::default(),
        };
        if let Some(h) = host {
            cfg.server.host = h;
        }
        if let Some(p) = port {
            cfg.server.port = p;
        }
        if let Some(d) = data_dir {
            cfg.storage.data_dir = d;
        }
        cfg.validate()?;
        Ok(cfg)
    }

    pub fn validate(&self) -> Result<()> {
        if self.server.max_body_bytes == 0 {
            return Err(FmError::invalid_input("max_body_bytes 必须 > 0"));
        }
        if self.index.max_text_bytes == 0 {
            return Err(FmError::invalid_input("max_text_bytes 必须 > 0"));
        }
        if self.index.rank_block == 0 {
            return Err(FmError::invalid_input("rank_block 必须 > 0"));
        }
        if self.index.sample_step == 0 {
            return Err(FmError::invalid_input("sample_step 必须 > 0"));
        }
        Ok(())
    }
}
