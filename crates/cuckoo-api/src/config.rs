//! 配置层：TOML 文件 + 环境变量覆盖。
//!
//! 优先级：环境变量 > TOML 文件 > 内置默认值。
//! 任何无法解释的配置都直接报错退出，不静默回退。

use std::fs;
use std::path::{Path, PathBuf};

use cuckoo_core::FilterParams;
use serde::Deserialize;

#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    pub params: FilterParams,
    pub rng_seed: u64,
    pub snapshot_path: PathBuf,
    pub master_key: Vec<u8>,
    /// 主密钥来源（仅用于日志展示，不输出密钥本身）。
    pub key_source: String,
}

#[derive(Debug, Deserialize, Default)]
struct FileConfig {
    #[serde(default)]
    server: ServerSection,
    #[serde(default)]
    filter: FilterSection,
    #[serde(default)]
    storage: StorageSection,
    #[serde(default)]
    security: SecuritySection,
}

#[derive(Debug, Deserialize)]
struct ServerSection {
    bind: Option<String>,
}
impl Default for ServerSection {
    fn default() -> Self {
        Self {
            bind: Some("127.0.0.1:8088".to_string()),
        }
    }
}

#[derive(Debug, Deserialize, Default)]
struct FilterSection {
    buckets_exp: Option<u32>,
    bucket_size: Option<u32>,
    fingerprint_bits: Option<u32>,
    max_kicks: Option<u32>,
    rng_seed: Option<u64>,
}

#[derive(Debug, Deserialize, Default)]
struct StorageSection {
    path: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
struct SecuritySection {
    master_key_file: Option<String>,
}

impl Config {
    /// 读取并校验配置。`config_path` 为 None 时仅使用环境变量/默认值。
    pub fn load(config_path: Option<&Path>) -> Result<Config, String> {
        let mut file_cfg = FileConfig::default();
        if let Some(path) = config_path {
            let raw = fs::read_to_string(path)
                .map_err(|e| format!("读取配置文件 {} 失败: {e}", path.display()))?;
            file_cfg = toml::from_str(&raw)
                .map_err(|e| format!("解析配置文件 {} 失败: {e}", path.display()))?;
        }

        let env = |key: &str| std::env::var(key).ok();

        let bind = env("CKF_BIND")
            .or(file_cfg.server.bind)
            .unwrap_or_else(|| "127.0.0.1:8088".to_string());

        let buckets_exp = env("CKF_BUCKETS_EXP")
            .map(|v| v.parse::<u32>())
            .transpose()
            .map_err(|e| format!("CKF_BUCKETS_EXP 解析失败: {e}"))?
            .or(file_cfg.filter.buckets_exp)
            .unwrap_or(12);
        let bucket_size = env("CKF_BUCKET_SIZE")
            .map(|v| v.parse::<u32>())
            .transpose()
            .map_err(|e| format!("CKF_BUCKET_SIZE 解析失败: {e}"))?
            .or(file_cfg.filter.bucket_size)
            .unwrap_or(4);
        let fingerprint_bits = env("CKF_FINGERPRINT_BITS")
            .map(|v| v.parse::<u32>())
            .transpose()
            .map_err(|e| format!("CKF_FINGERPRINT_BITS 解析失败: {e}"))?
            .or(file_cfg.filter.fingerprint_bits)
            .unwrap_or(12);
        let max_kicks = env("CKF_MAX_KICKS")
            .map(|v| v.parse::<u32>())
            .transpose()
            .map_err(|e| format!("CKF_MAX_KICKS 解析失败: {e}"))?
            .or(file_cfg.filter.max_kicks)
            .unwrap_or(500);
        let rng_seed = env("CKF_RNG_SEED")
            .map(|v| v.parse::<u64>())
            .transpose()
            .map_err(|e| format!("CKF_RNG_SEED 解析失败: {e}"))?
            .or(file_cfg.filter.rng_seed)
            .unwrap_or(20260927);

        let snapshot_path = PathBuf::from(
            env("CKF_SNAPSHOT_PATH")
                .or(file_cfg.storage.path)
                .unwrap_or_else(|| "data/filter.snapshot".to_string()),
        );

        // 主密钥：环境变量优先，其次文件；二者都没有则报错（拒绝使用硬编码默认密钥）。
        let (master_key, key_source) = if let Some(k) = env("CKF_MASTER_KEY") {
            (k.into_bytes(), "env:CKF_MASTER_KEY".to_string())
        } else if let Some(key_file) =
            env("CKF_MASTER_KEY_FILE").or(file_cfg.security.master_key_file)
        {
            let raw = fs::read(&key_file)
                .map_err(|e| format!("读取主密钥文件 {key_file} 失败: {e}"))?;
            // 允许文件末尾换行。
            let trimmed = raw
                .strip_suffix(b"\n")
                .map(|b| b.strip_suffix(b"\r").unwrap_or(b).to_vec())
                .unwrap_or(raw);
            (trimmed, format!("file:{key_file}"))
        } else {
            return Err(
                "未配置删除凭证主密钥：请设置 CKF_MASTER_KEY 或 CKF_MASTER_KEY_FILE"
                    .to_string(),
            );
        };
        if master_key.len() < 16 {
            return Err(format!(
                "主密钥至少需要 16 字节，当前 {} 字节",
                master_key.len()
            ));
        }

        let params = FilterParams::new(buckets_exp, bucket_size, fingerprint_bits, max_kicks)
            .map_err(|e| e.to_string())?;

        Ok(Config {
            bind,
            params,
            rng_seed,
            snapshot_path,
            master_key,
            key_source,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    // 环境变量是进程级共享状态，测试间加锁串行执行。
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn base_env_cleanup() {
        for k in [
            "CKF_BIND",
            "CKF_BUCKETS_EXP",
            "CKF_BUCKET_SIZE",
            "CKF_FINGERPRINT_BITS",
            "CKF_MAX_KICKS",
            "CKF_RNG_SEED",
            "CKF_SNAPSHOT_PATH",
            "CKF_MASTER_KEY",
            "CKF_MASTER_KEY_FILE",
        ] {
            std::env::remove_var(k);
        }
    }

    #[test]
    fn requires_master_key() {
        let _g = ENV_LOCK.lock().unwrap();
        base_env_cleanup();
        let err = Config::load(None).unwrap_err();
        assert!(err.contains("主密钥"), "{err}");
    }

    #[test]
    fn env_overrides_defaults() {
        let _g = ENV_LOCK.lock().unwrap();
        base_env_cleanup();
        std::env::set_var("CKF_MASTER_KEY", "0123456789abcdef");
        std::env::set_var("CKF_BIND", "127.0.0.1:9999");
        std::env::set_var("CKF_MAX_KICKS", "7");
        let cfg = Config::load(None).unwrap();
        assert_eq!(cfg.bind, "127.0.0.1:9999");
        assert_eq!(cfg.params.max_kicks, 7);
        assert_eq!(cfg.params.bucket_size, 4);
        base_env_cleanup();
    }

    #[test]
    fn rejects_short_key_and_bad_params() {
        let _g = ENV_LOCK.lock().unwrap();
        base_env_cleanup();
        std::env::set_var("CKF_MASTER_KEY", "short");
        assert!(Config::load(None).is_err());
        base_env_cleanup();
        std::env::set_var("CKF_MASTER_KEY", "0123456789abcdef");
        std::env::set_var("CKF_BUCKET_SIZE", "3");
        assert!(Config::load(None).is_err());
        base_env_cleanup();
    }
}
