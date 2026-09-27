//! Configuration for builds and the server.
//!
//! Server config is a small TOML file; every field has a default so an empty
//! config (or none at all) is valid. CLI flags override file values.

use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::error::{MphfError, Result};
use crate::index::VerifyMode;

/// Server-side configuration (TOML).
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct ServerConfig {
    /// Directory containing `<set>.mphf` index files.
    pub data_dir: PathBuf,
    /// Bind address.
    pub bind: String,
    /// Default fingerprint width applied to HTTP build requests that omit it
    /// (0 = full-key verification).
    pub fingerprint_bits: u8,
    pub load_factor: f64,
    pub max_attempts: u32,
    pub base_seed: u64,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            data_dir: PathBuf::from("data"),
            bind: "127.0.0.1:8080".into(),
            fingerprint_bits: 16,
            load_factor: 0.75,
            max_attempts: 256,
            base_seed: 1,
        }
    }
}

impl ServerConfig {
    pub fn load(path: Option<&Path>) -> Result<ServerConfig> {
        match path {
            None => Ok(ServerConfig::default()),
            Some(p) => {
                let text = std::fs::read_to_string(p)?;
                let cfg: ServerConfig = toml::from_str(&text).map_err(|e| {
                    MphfError::config(format!("invalid config {}: {e}", p.display()))
                })?;
                cfg.validate()?;
                Ok(cfg)
            }
        }
    }

    pub fn validate(&self) -> Result<()> {
        if !(0.05..=0.81).contains(&self.load_factor) {
            return Err(MphfError::config(format!(
                "load_factor {} outside (0.05, 0.81]",
                self.load_factor
            )));
        }
        if self.max_attempts == 0 {
            return Err(MphfError::config("max_attempts must be >= 1"));
        }
        match self.fingerprint_bits {
            0 | 8 | 16 | 32 | 64 => {}
            other => {
                return Err(MphfError::config(format!(
                    "fingerprint_bits must be 0/8/16/32/64, got {other}"
                )))
            }
        }
        Ok(())
    }

    pub fn verify_mode(&self) -> Result<VerifyMode> {
        VerifyMode::parse(self.fingerprint_bits)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_valid() {
        assert!(ServerConfig::default().validate().is_ok());
    }

    #[test]
    fn rejects_bad_load_factor_and_bits() {
        let mut c = ServerConfig::default();
        c.load_factor = 0.95;
        assert!(c.validate().is_err());
        c.load_factor = 0.81;
        c.fingerprint_bits = 7;
        assert!(c.validate().is_err());
    }

    #[test]
    fn parses_toml() {
        let toml = "data_dir = \"/tmp/x\"\nbind = \"0.0.0.0:9000\"\n";
        let c: ServerConfig = toml::from_str(toml).unwrap();
        assert_eq!(c.data_dir, PathBuf::from("/tmp/x"));
        assert_eq!(c.bind, "0.0.0.0:9000");
        // Defaults for omitted fields.
        assert_eq!(c.fingerprint_bits, 16);
    }
}
