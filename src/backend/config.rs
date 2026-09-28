//! 启动配置。
//!
//! 加载顺序（后者覆盖前者）：
//! 1. 内置默认值；
//! 2. `ROBDD_CONFIG` 指向的 JSON 文件（默认 `config/default.json`，存在才读）；
//! 3. 环境变量 `ROBDD_BIND`、`ROBDD_VAR_CAP`、`ROBDD_LOG_LEVEL`；
//! 4. 命令行 `--bind / --var-cap / --log-level`。
//!
//! 配置文件解析只依赖 `serde_json`，不引入额外配置库。

use serde::Deserialize;

/// 后端运行配置。
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Config {
    /// 监听地址（`host:port`，端口 0 表示由系统分配）。
    pub bind: String,
    /// 等价穷举真值表的变量数上限。
    pub var_cap: usize,
    /// 日志级别（error/warn/info/debug/trace/off）。
    pub log_level: String,
    /// 等价请求是否默认对表达式做脱敏。
    pub redact_by_default: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            bind: "127.0.0.1:8080".to_string(),
            var_cap: 20,
            log_level: "info".to_string(),
            redact_by_default: false,
        }
    }
}

/// 配置加载失败（属于环境问题，不归类为请求错误）。
#[derive(Debug)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "configuration error: {}", self.0)
    }
}

impl std::error::Error for ConfigError {}

impl Config {
    /// 按文档约定的顺序加载配置。
    pub fn load(cli_args: &[String]) -> Result<Config, ConfigError> {
        let mut cfg = Config::default();

        // 2) JSON 文件。
        let path =
            std::env::var("ROBDD_CONFIG").unwrap_or_else(|_| "config/default.json".to_string());
        if let Ok(body) = std::fs::read_to_string(&path) {
            let file_cfg: Config = serde_json::from_str(&body)
                .map_err(|e| ConfigError(format!("failed to parse {path}: {e}")))?;
            cfg = file_cfg;
        }

        // 3) 环境变量。
        if let Ok(v) = std::env::var("ROBDD_BIND") {
            cfg.bind = v;
        }
        if let Ok(v) = std::env::var("ROBDD_VAR_CAP") {
            cfg.var_cap = v
                .parse()
                .map_err(|_| ConfigError(format!("invalid ROBDD_VAR_CAP={v}")))?;
        }
        if let Ok(v) = std::env::var("ROBDD_LOG_LEVEL") {
            cfg.log_level = v;
        }
        if let Ok(v) = std::env::var("ROBDD_REDACT") {
            cfg.redact_by_default = v == "1" || v.eq_ignore_ascii_case("true");
        }

        // 4) 极简命令行参数：--bind V / --var-cap V / --log-level V。
        let mut i = 0;
        while i < cli_args.len() {
            let set = |key: &str,
                       args: &[String],
                       idx: &mut usize,
                       cfg: &mut Config|
             -> Result<(), ConfigError> {
                let value = args
                    .get(*idx + 1)
                    .ok_or_else(|| ConfigError(format!("missing value for {key}")))?;
                match key {
                    "--bind" => cfg.bind = value.clone(),
                    "--var-cap" => {
                        cfg.var_cap = value
                            .parse()
                            .map_err(|_| ConfigError(format!("invalid --var-cap {value}")))?
                    }
                    "--log-level" => cfg.log_level = value.clone(),
                    other => return Err(ConfigError(format!("unknown argument {other}"))),
                }
                *idx += 2;
                Ok(())
            };
            match cli_args[i].as_str() {
                key @ ("--bind" | "--var-cap" | "--log-level") => {
                    set(key, cli_args, &mut i, &mut cfg)?
                }
                other => return Err(ConfigError(format!("unknown argument {other}"))),
            }
        }

        if cfg.var_cap > 63 {
            return Err(ConfigError(
                "var-cap must be <= 63 (truth tables are sized 2^n)".into(),
            ));
        }
        Ok(cfg)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_safe() {
        let cfg = Config::default();
        assert_eq!(cfg.bind, "127.0.0.1:8080");
        assert_eq!(cfg.var_cap, 20);
    }

    #[test]
    fn cli_overrides_are_parsed_and_validated() {
        let cfg = Config::load(&[
            "--bind".to_string(),
            "0.0.0.0:9999".to_string(),
            "--var-cap".to_string(),
            "8".to_string(),
        ])
        .unwrap();
        assert_eq!(cfg.bind, "0.0.0.0:9999");
        assert_eq!(cfg.var_cap, 8);
        assert!(Config::load(&["--var-cap".to_string(), "64".to_string()]).is_err());
        assert!(Config::load(&["--bogus".to_string()]).is_err());
    }
}
