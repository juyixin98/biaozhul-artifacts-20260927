//! 配置层：环境变量 + CLI `--key value` / `--key=value` 覆盖。
//! 不引入配置框架；所有默认值集中在此，测试可直接构造。

use std::collections::HashMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// WAL 所在目录。
    pub data_dir: String,
    /// 监听地址。
    pub bind: String,
    /// 单请求体上限（字节）。
    pub max_body_bytes: usize,
    /// 日志过滤（tracing-subscriber EnvFilter 语法）。
    pub log_level: String,
}

impl Default for Config {
    fn default() -> Config {
        Config {
            data_dir: "./data".into(),
            bind: "127.0.0.1:8080".into(),
            max_body_bytes: 1_048_576,
            log_level: "info".into(),
        }
    }
}

fn env_or(key: &str, default: String) -> String {
    std::env::var(key).unwrap_or(default)
}

impl Config {
    /// 环境变量 → 默认值；CLI 参数最后覆盖。
    pub fn load(cli_args: &[String]) -> Result<Config, String> {
        let mut cfg = Config {
            data_dir: env_or("PR2D_DATA_DIR", "./data".into()),
            bind: env_or("PR2D_BIND", "127.0.0.1:8080".into()),
            max_body_bytes: env_or("PR2D_MAX_BODY_BYTES", "1048576".into())
                .parse()
                .map_err(|_| "PR2D_MAX_BODY_BYTES must be a positive integer".to_string())?,
            log_level: env_or("PR2D_LOG_LEVEL", "info".into()),
        };
        if cfg.max_body_bytes == 0 {
            return Err("PR2D_MAX_BODY_BYTES must be > 0".into());
        }

        // 精确匹配白名单键；`--k=v` 与 `--k v` 两种形式都支持。
        const KEYS: [&str; 4] = ["data-dir", "bind", "max-body-bytes", "log-level"];
        let mut flat = HashMap::new();
        let mut i = 0;
        while i < cli_args.len() {
            let a = &cli_args[i];
            let (k, inline) = match a.split_once('=') {
                Some((flag, v)) => (flag.trim_start_matches('-'), Some(v.to_string())),
                None => (a.trim_start_matches('-'), None),
            };
            if !KEYS.contains(&k) {
                return Err(format!("unknown argument: {a}"));
            }
            let v = match inline {
                Some(v) => {
                    i += 1;
                    v
                }
                None => {
                    let v = cli_args
                        .get(i + 1)
                        .ok_or_else(|| format!("missing value for {a}"))?
                        .clone();
                    i += 2;
                    v
                }
            };
            flat.insert(k.to_string(), v);
        }
        if let Some(v) = flat.remove("data-dir") {
            cfg.data_dir = v;
        }
        if let Some(v) = flat.remove("bind") {
            cfg.bind = v;
        }
        if let Some(v) = flat.remove("max-body-bytes") {
            cfg.max_body_bytes = v
                .parse()
                .map_err(|_| "--max-body-bytes must be an integer")?;
            if cfg.max_body_bytes == 0 {
                return Err("--max-body-bytes must be > 0".into());
            }
        }
        if let Some(v) = flat.remove("log-level") {
            cfg.log_level = v;
        }
        Ok(cfg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_overrides_env() {
        let cfg = Config::load(&[
            "--bind".into(),
            "0.0.0.0:9999".into(),
            "--data-dir=/tmp/x".into(),
        ])
        .unwrap();
        assert_eq!(cfg.bind, "0.0.0.0:9999");
        assert_eq!(cfg.data_dir, "/tmp/x");
    }

    #[test]
    fn rejects_bad_args() {
        assert!(Config::load(&["--nope".into()]).is_err());
        assert!(Config::load(&["--bind".into()]).is_err());
        assert!(Config::load(&["--max-body-bytes=0".into()]).is_err());
    }
}
