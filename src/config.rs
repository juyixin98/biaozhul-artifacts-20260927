//! 配置模型、TOML 加载、环境变量覆盖与参数校验。
//!
//! 配置是显式分层：内置默认值 -> `config/default.toml`（或 --config 指定文件）
//! -> 环境变量 `CF_*`。任何非法参数都会在启动期报错，而不是在运行期静默退化。

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::hashing::KernelParams;
use crate::KERNEL_VERSION;

/// 顶层配置。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct Config {
    pub server: ServerConfig,
    pub filter: FilterConfig,
    pub storage: StorageConfig,
    pub credentials: CredentialsConfig,
    pub logging: LoggingConfig,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    pub max_request_bytes: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct FilterConfig {
    pub num_buckets: u64,
    pub bucket_size: u32,
    pub fingerprint_bits: u32,
    pub max_kicks: u32,
    pub seed_hex: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct StorageConfig {
    pub data_dir: PathBuf,
    pub snapshot_file: String,
    pub fsync: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct CredentialsConfig {
    /// 32 字节十六进制；空串表示从 `<data_dir>/secret.key` 加载/生成。
    pub hmac_secret_hex: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct LoggingConfig {
    pub level: String,
    pub format: String,
    pub run_id: String,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_string(),
            port: 8080,
            max_request_bytes: 1 << 20,
        }
    }
}

impl Default for FilterConfig {
    fn default() -> Self {
        Self {
            num_buckets: 1024,
            bucket_size: 4,
            fingerprint_bits: 16,
            max_kicks: 500,
            seed_hex: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                .to_string(),
        }
    }
}

impl Default for StorageConfig {
    fn default() -> Self {
        Self {
            data_dir: PathBuf::from("./data"),
            snapshot_file: "snapshot.bin".to_string(),
            fsync: true,
        }
    }
}

impl Default for LoggingConfig {
    fn default() -> Self {
        Self {
            level: "info".to_string(),
            format: "text".to_string(),
            run_id: String::new(),
        }
    }
}

/// 配置加载/校验错误。
#[derive(Debug)]
pub enum ConfigError {
    /// 读取或解析配置文件失败。
    Io(String),
    /// 参数不合法：(字段, 原因)。
    Invalid(String, String),
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Io(e) => write!(f, "配置文件读取失败: {e}"),
            ConfigError::Invalid(field, why) => write!(f, "配置项 {field} 非法: {why}"),
        }
    }
}

impl std::error::Error for ConfigError {}

impl Config {
    /// 加载内置默认值，再用给定 TOML 文件（若存在）覆盖，最后应用环境变量。
    ///
    /// `path` 为 None 时完全跳过文件层。文件不存在是显式错误而非静默忽略，
    /// 以防部署时路径拼错导致意外使用默认值。
    pub fn load(path: Option<&std::path::Path>) -> Result<Config, ConfigError> {
        let mut cfg = Config::default();
        if let Some(p) = path {
            let text = std::fs::read_to_string(p).map_err(|e| ConfigError::Io(e.to_string()))?;
            cfg = toml::from_str(&text).map_err(|e| ConfigError::Io(e.to_string()))?;
        }
        cfg.apply_env();
        cfg.validate()?;
        Ok(cfg)
    }

    /// 环境变量覆盖。仅覆盖少量运维常用项；结构参数以文件为准。
    fn apply_env(&mut self) {
        if let Ok(v) = std::env::var("CF_HOST") {
            self.server.host = v;
        }
        if let Ok(v) = std::env::var("CF_PORT") {
            if let Ok(port) = v.parse() {
                self.server.port = port;
            }
        }
        if let Ok(v) = std::env::var("CF_DATA_DIR") {
            self.storage.data_dir = PathBuf::from(v);
        }
        if let Ok(v) = std::env::var("CF_FSYNC") {
            if let Ok(b) = v.parse() {
                self.storage.fsync = b;
            }
        }
        if let Ok(v) = std::env::var("CF_HMAC_SECRET_HEX") {
            self.credentials.hmac_secret_hex = v;
        }
        if let Ok(v) = std::env::var("CF_LOG_LEVEL") {
            self.logging.level = v;
        }
        if let Ok(v) = std::env::var("CF_RUN_ID") {
            self.logging.run_id = v;
        }
        if let Ok(v) = std::env::var("CF_FILTER_SEED_HEX") {
            self.filter.seed_hex = v;
        }
    }

    /// 启动期参数校验。任何不满足内核前置条件的参数都直接拒绝启动。
    pub fn validate(&self) -> Result<(), ConfigError> {
        let f = &self.filter;
        if f.num_buckets < 2 || !f.num_buckets.is_power_of_two() {
            return Err(ConfigError::Invalid(
                "filter.num_buckets".into(),
                format!(
                    "{} 不是 >=2 的 2 的幂（备用桶异或定位的硬性要求）",
                    f.num_buckets
                ),
            ));
        }
        if !(1..=16).contains(&f.bucket_size) {
            return Err(ConfigError::Invalid(
                "filter.bucket_size".into(),
                format!("{} 不在 [1,16]", f.bucket_size),
            ));
        }
        if !(4..=32).contains(&f.fingerprint_bits) {
            return Err(ConfigError::Invalid(
                "filter.fingerprint_bits".into(),
                format!("{} 不在 [4,32]", f.fingerprint_bits),
            ));
        }
        if f.max_kicks > 100_000 {
            return Err(ConfigError::Invalid(
                "filter.max_kicks".into(),
                format!("{} 超过 100000 上限", f.max_kicks),
            ));
        }
        let seed = decode_hex_32(&f.seed_hex)
            .map_err(|e| ConfigError::Invalid("filter.seed_hex".into(), e))?;
        // 全零种子虽可用但通常是配置失误，明确拒绝。
        if seed.iter().all(|b| *b == 0) {
            return Err(ConfigError::Invalid(
                "filter.seed_hex".into(),
                "全零种子不允许".into(),
            ));
        }
        if self.server.max_request_bytes < 64 {
            return Err(ConfigError::Invalid(
                "server.max_request_bytes".into(),
                "小于 64 字节".into(),
            ));
        }
        if !["trace", "debug", "info", "warn", "error"].contains(&self.logging.level.as_str()) {
            return Err(ConfigError::Invalid(
                "logging.level".into(),
                format!("{} 不是合法日志级别", self.logging.level),
            ));
        }
        if !["text", "json"].contains(&self.logging.format.as_str()) {
            return Err(ConfigError::Invalid(
                "logging.format".into(),
                format!("{} 不是 text/json", self.logging.format),
            ));
        }
        if self.storage.snapshot_file.contains('/') || self.storage.snapshot_file.is_empty() {
            return Err(ConfigError::Invalid(
                "storage.snapshot_file".into(),
                "必须是 data_dir 下的纯文件名".into(),
            ));
        }
        if !self.credentials.hmac_secret_hex.is_empty() {
            decode_hex_32(&self.credentials.hmac_secret_hex)
                .map_err(|e| ConfigError::Invalid("credentials.hmac_secret_hex".into(), e))?;
        }
        Ok(())
    }

    /// 解析 32 字节种子。
    pub fn kernel_seed(&self) -> [u8; 32] {
        decode_hex_32(&self.filter.seed_hex).expect("validate 已保证")
    }

    /// 构造内核参数包。
    pub fn kernel_params(&self) -> KernelParams {
        KernelParams {
            version: KERNEL_VERSION,
            num_buckets: self.filter.num_buckets,
            bucket_size: self.filter.bucket_size,
            fingerprint_bits: self.filter.fingerprint_bits,
            max_kicks: self.filter.max_kicks,
            seed: self.kernel_seed(),
        }
    }
}

/// 解码 64 位十六进制为 32 字节数组。
pub fn decode_hex_32(s: &str) -> Result<[u8; 32], String> {
    let trimmed = s.trim();
    if trimmed.len() != 64 {
        return Err(format!(
            "需要 64 个十六进制字符（32 字节），实际 {} 个",
            trimmed.len()
        ));
    }
    let mut out = [0u8; 32];
    hex::decode_to_slice(trimmed, &mut out).map_err(|e| format!("十六进制解码失败: {e}"))?;
    Ok(out)
}
