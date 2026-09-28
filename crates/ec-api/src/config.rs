//! Service configuration (JSON file, see `config/service.example.json`).
//!
//! Kept as plain data with [`ServerConfig::load`] so the same struct serves
//! the binary, tests and documentation.

use std::path::Path;

use ec_core::error::{EcError, EcResult};
use serde::{Deserialize, Serialize};

/// All knobs the service has; intentionally tiny and explicit.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerConfig {
    /// Socket address, e.g. `127.0.0.1:8080`.
    pub listen: String,
    /// Directory backing the filesystem store.
    pub storage_dir: String,
    /// If true, request bodies/base64 shards are accepted; purely a
    /// deployment-time safety switch for read-only test instances.
    #[serde(default = "default_true")]
    pub allow_writes: bool,
    /// Fixed coding parameters for new objects.
    pub k: u16,
    pub m: u16,
    /// Log filter ("info" by default).
    #[serde(default = "default_log")]
    pub log_level: String,
}

fn default_true() -> bool {
    true
}
fn default_log() -> String {
    "info".into()
}

impl ServerConfig {
    /// Read + parse a JSON config file.
    pub fn load(path: impl AsRef<Path>) -> EcResult<Self> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path).map_err(|e| {
            EcError::Store(format!("reading config {}: {e}", path.display()))
        })?;
        let cfg: ServerConfig = serde_json::from_str(&text).map_err(|e| {
            EcError::Internal(format!("parsing config {}: {e}", path.display()))
        })?;
        cfg.validate()?;
        Ok(cfg)
    }

    /// Validate every field, including the coding parameters (delegated to
    /// the kernel so one rule set applies everywhere).
    pub fn validate(&self) -> EcResult<()> {
        if self.listen.is_empty() {
            return Err(EcError::Internal("listen must not be empty".into()));
        }
        if self.storage_dir.is_empty() {
            return Err(EcError::Internal("storage_dir must not be empty".into()));
        }
        // Coding parameters validated by the kernel.
        let _ = ec_core::CodecConfig::new(self.k, self.m)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_tmp(name: &str, body: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "ec-cfg-{}-{}-{name}.json",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::write(&p, body).unwrap();
        p
    }

    #[test]
    fn loads_valid_config_with_defaults() {
        let p = write_tmp(
            "ok",
            r#"{"listen":"127.0.0.1:9000","storage_dir":"/tmp/x","k":3,"m":2}"#,
        );
        let cfg = ServerConfig::load(&p).unwrap();
        assert_eq!(cfg.listen, "127.0.0.1:9000");
        assert!(cfg.allow_writes); // defaulted
        assert_eq!(cfg.log_level, "info");
    }

    #[test]
    fn rejects_invalid_coding_params_via_kernel() {
        let p = write_tmp(
            "bad",
            r#"{"listen":"a","storage_dir":"/tmp/x","k":200,"m":56}"#,
        );
        let err = ServerConfig::load(&p).unwrap_err();
        assert_eq!(err.code(), "INVALID_CONFIG");
    }

    #[test]
    fn rejects_malformed_json_and_empty_fields() {
        let p = write_tmp("malformed", "{ not json");
        assert!(matches!(ServerConfig::load(&p).unwrap_err(), EcError::Internal(_)));
        let p = write_tmp(
            "emptylisten",
            r#"{"listen":"","storage_dir":"/tmp/x","k":1,"m":1}"#,
        );
        assert!(matches!(ServerConfig::load(&p).unwrap_err(), EcError::Internal(_)));
    }
}
