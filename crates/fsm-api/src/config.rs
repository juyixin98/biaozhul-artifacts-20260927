//! Runtime configuration.
//!
//! Sources, later ones win: built-in defaults, then `config.env`-style
//! environment variables. Kept dependency-free (no config crate): the
//! surface is deliberately tiny.

#[derive(Debug, Clone)]
pub struct Config {
    pub bind: String,
    pub default_max_states: u64,
    pub log_level: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            bind: "127.0.0.1:8080".into(),
            default_max_states: 100_000,
            log_level: "info".into(),
        }
    }
}

impl Config {
    pub fn from_env() -> Self {
        let mut c = Config::default();
        if let Ok(v) = std::env::var("FSM_BIND") {
            c.bind = v;
        }
        if let Ok(v) = std::env::var("FSM_MAX_STATES") {
            if let Ok(n) = v.parse() {
                c.default_max_states = n;
            }
        }
        if let Ok(v) = std::env::var("RUST_LOG") {
            c.log_level = v;
        }
        c
    }
}
