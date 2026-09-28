//! Service configuration.
//!
//! File format is a deliberately small, flat subset of TOML: one
//! `key = value` pair per line, `#` comments, values optionally quoted.
//! Supported keys: `listen`, `data_dir`, `log_level`. An unknown or
//! malformed entry is an error rather than silently ignored.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::str::FromStr;

use crate::error::WmError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub listen: SocketAddr,
    pub data_dir: PathBuf,
    pub log_level: String,
}

impl Config {
    pub fn defaults() -> Self {
        Config {
            listen: "127.0.0.1:18080".parse().expect("valid default addr"),
            data_dir: PathBuf::from("./data"),
            log_level: "info".to_string(),
        }
    }

    /// Parse config text. Exposed (and tested) independently of the file
    /// system so the format itself is verifiable.
    pub fn parse(text: &str) -> Result<Self, WmError> {
        let mut cfg = Config::defaults();
        for (lineno, raw) in text.lines().enumerate() {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            // Strip an inline ` #` comment unless the `#` sits inside a
            // quoted value (odd number of quote chars before it).
            let line = if let Some(h) = line.find(" #") {
                let before = &line[..h];
                let in_quotes =
                    before.matches('"').count() % 2 == 1 || before.matches('\'').count() % 2 == 1;
                if in_quotes {
                    line
                } else {
                    &line[..h]
                }
            } else {
                line
            };
            let (key, value) = line.split_once('=').ok_or_else(|| {
                WmError::BadRequest(format!(
                    "config line {}: expected `key = value`",
                    lineno + 1
                ))
            })?;
            let key = key.trim();
            let value = strip_quotes(value.trim());
            match key {
                "listen" => {
                    cfg.listen = SocketAddr::from_str(&value).map_err(|e| {
                        WmError::BadRequest(format!(
                            "config line {}: invalid listen address: {e}",
                            lineno + 1
                        ))
                    })?;
                }
                "data_dir" => {
                    if value.is_empty() {
                        return Err(WmError::BadRequest(format!(
                            "config line {}: data_dir is empty",
                            lineno + 1
                        )));
                    }
                    cfg.data_dir = PathBuf::from(value);
                }
                "log_level" => cfg.log_level = value,
                other => {
                    return Err(WmError::BadRequest(format!(
                        "config line {}: unknown key {other:?}",
                        lineno + 1
                    )))
                }
            }
        }
        Ok(cfg)
    }

    pub fn load(path: &std::path::Path) -> Result<Self, WmError> {
        match std::fs::read_to_string(path) {
            Ok(text) => Self::parse(&text),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(WmError::Io(format!(
                "config file not found: {}",
                path.display()
            ))),
            Err(e) => Err(WmError::Io(format!("read {}: {e}", path.display()))),
        }
    }
}

fn strip_quotes(v: &str) -> String {
    let b = v.as_bytes();
    if b.len() >= 2
        && ((b[0] == b'"' && b[b.len() - 1] == b'"') || (b[0] == b'\'' && b[b.len() - 1] == b'\''))
    {
        v[1..v.len() - 1].to_string()
    } else {
        v.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_config_with_comments_and_quotes() {
        let text = r#"
            # service
            listen = "127.0.0.1:9090"
            data_dir = '/var/lib/wm'
            log_level = debug  # inline comment
        "#;
        let cfg = Config::parse(text).unwrap();
        assert_eq!(cfg.listen, "127.0.0.1:9090".parse().unwrap());
        assert_eq!(cfg.data_dir, PathBuf::from("/var/lib/wm"));
        assert_eq!(cfg.log_level, "debug");
    }

    #[test]
    fn empty_text_gives_defaults() {
        assert_eq!(Config::parse("").unwrap(), Config::defaults());
    }

    #[test]
    fn unknown_key_and_bad_value_are_errors() {
        assert!(matches!(
            Config::parse("port = 80"),
            Err(WmError::BadRequest(_))
        ));
        assert!(matches!(
            Config::parse("listen = not-an-addr"),
            Err(WmError::BadRequest(_))
        ));
        assert!(matches!(
            Config::parse("garbage line without equals"),
            Err(WmError::BadRequest(_))
        ));
    }
}
