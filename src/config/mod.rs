//! File/environment configuration.
//!
//! Optional `config.toml`; every field has a default and the operational knobs accept
//! environment overrides so the service also runs from an empty directory with zero
//! configuration.

use crate::solver::registry::SolverSpec;

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(default)]
pub struct Config {
    pub bind_addr: String,
    pub default_solver: String,
    pub verifier_solver: String,
    pub brute_max_vars: usize,
    pub default_call_budget: Option<usize>,
    pub default_decision_budget: Option<u64>,
    pub external_timeout_ms: u64,
    pub job_ttl_seconds: u64,
    pub solvers: Vec<SolverSpec>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            bind_addr: "127.0.0.1:8080".into(),
            default_solver: "dpll".into(),
            verifier_solver: "brute".into(),
            brute_max_vars: 22,
            default_call_budget: Some(1024),
            default_decision_budget: Some(200_000),
            external_timeout_ms: 30_000,
            job_ttl_seconds: 3600,
            solvers: Vec::new(),
        }
    }
}

impl Config {
    /// Load `config.toml` if present, apply defaults, then environment overrides.
    pub fn load(path: &str) -> Result<Self, String> {
        let mut cfg = match std::fs::read_to_string(path) {
            Ok(contents) => toml::from_str::<Config>(&contents)
                .map_err(|e| format!("invalid {path}: {e}"))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Config::default(),
            Err(e) => return Err(format!("cannot read {path}: {e}")),
        };
        cfg.apply_env();
        Ok(cfg)
    }

    /// Config constructed purely from defaults + environment (no file required).
    pub fn load_effective() -> Self {
        let mut cfg = Config::default();
        cfg.apply_env();
        cfg
    }

    fn apply_env(&mut self) {
        if let Ok(v) = std::env::var("MUS_BIND_ADDR") {
            self.bind_addr = v;
        }
        if let Ok(v) = std::env::var("MUS_DEFAULT_SOLVER") {
            self.default_solver = v;
        }
        if let Ok(v) = std::env::var("MUS_VERIFIER_SOLVER") {
            self.verifier_solver = v;
        }
        if let Ok(v) = std::env::var("MUS_BRUTE_MAX_VARS") {
            if let Ok(n) = v.parse() {
                self.brute_max_vars = n;
            }
        }
        if let Ok(v) = std::env::var("MUS_CALL_BUDGET") {
            self.default_call_budget = v.parse().ok();
        }
        if let Ok(v) = std::env::var("MUS_DECISION_BUDGET") {
            self.default_decision_budget = v.parse().ok();
        }
        if let Ok(v) = std::env::var("MUS_EXTERNAL_TIMEOUT_MS") {
            if let Ok(n) = v.parse() {
                self.external_timeout_ms = n;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_sane() {
        let c = Config::default();
        assert_eq!(c.default_solver, "dpll");
        assert_eq!(c.verifier_solver, "brute");
    }

    #[test]
    fn missing_file_is_not_an_error() {
        let c = Config::load("/nonexistent/mus/config.toml").unwrap();
        assert!(!c.bind_addr.is_empty());
    }

    #[test]
    fn parses_external_solver_spec() {
        let toml_src = r#"
[[solvers]]
kind = "external"
name = "kissat"
argv = ["kissat", "{in}"]
"#;
        let c: Config = toml::from_str(toml_src).unwrap();
        assert_eq!(c.solvers.len(), 1);
        match &c.solvers[0] {
            SolverSpec::External { name, argv } => {
                assert_eq!(name, "kissat");
                assert_eq!(argv[1], "{in}");
            }
            other => panic!("unexpected spec: {other:?}"),
        }
    }
}
