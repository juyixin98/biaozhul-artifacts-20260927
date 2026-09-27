//! Runtime configuration, loaded exclusively from environment variables with
//! safe local defaults. No external services are required.

use std::time::SystemTime;

#[derive(Debug, Clone)]
pub struct Config {
    /// Directory containing object directories (`<data_dir>/<object_id>/...`).
    pub data_dir: String,
    /// TCP bind address for Axum.
    pub bind_addr: String,
    /// Maximum accepted object size in bytes.
    pub max_object_bytes: usize,
    /// Allowed `(k, m)` pairs. Empty means the built-in small set is used.
    pub allowed_profiles: Vec<(u8, u8)>,
    /// Process start marker, used in log context.
    pub started_unix: u64,
}

/// The "enumerable small configurations": every pair in this list is fully
/// covered by the exhaustive erasure-combination tests.
pub const BUILTIN_PROFILES: &[(u8, u8)] = &[
    (1, 1),
    (2, 1),
    (3, 2),
    (4, 2),
];

impl Config {
    pub fn from_env() -> Self {
        let data_dir = std::env::var("EC_DATA_DIR").unwrap_or_else(|_| "./ec-data".to_string());
        let bind_addr =
            std::env::var("EC_BIND_ADDR").unwrap_or_else(|_| "127.0.0.1:8080".to_string());
        let max_object_bytes = std::env::var("EC_MAX_OBJECT_BYTES")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(64 * 1024 * 1024);
        let started_unix = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        // Optional override: EC_PROFILES="2,1 3,2"
        let allowed_profiles = std::env::var("EC_PROFILES")
            .ok()
            .map(|s| parse_profiles(&s))
            .unwrap_or_else(|| BUILTIN_PROFILES.to_vec());
        Self {
            data_dir,
            bind_addr,
            max_object_bytes,
            allowed_profiles,
            started_unix,
        }
    }
}

fn parse_profiles(s: &str) -> Vec<(u8, u8)> {
    s.split_whitespace()
        .filter_map(|pair| {
            let mut it = pair.split(',');
            let k = it.next()?.trim().parse().ok()?;
            let m = it.next()?.trim().parse().ok()?;
            Some((k, m))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_profile_override() {
        assert_eq!(parse_profiles("2,1 3,2"), vec![(2, 1), (3, 2)]);
        assert!(parse_profiles("garbage").is_empty());
    }
}
