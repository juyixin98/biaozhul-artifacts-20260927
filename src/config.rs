use serde::{Deserialize, Serialize};

/// Fixpoint / execution knobs. Loaded from `config/default.toml`; the HTTP API
/// may override individual fields per request.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    /// Number of plain join iterations before widening starts.
    pub widen_delay: usize,
    /// Maximum number of narrowing passes after fixpoint convergence.
    pub narrow_iters: usize,
    /// Whether narrowing is applied at all.
    pub enable_narrowing: bool,
    /// Safety valve for chaotic iteration.
    pub max_iterations: usize,
    /// Statement fuel for the concrete interpreter.
    pub concrete_fuel: usize,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            widen_delay: 1,
            narrow_iters: 2,
            enable_narrowing: true,
            max_iterations: 200,
            concrete_fuel: 200_000,
        }
    }
}

impl Config {
    pub fn load_toml(text: &str) -> Result<Config, String> {
        let raw: toml::Value = text
            .parse()
            .map_err(|e| format!("invalid TOML config: {e}"))?;
        let mut cfg = Config::default();
        let table = match raw {
            toml::Value::Table(t) => t,
            _ => return Err("config root must be a TOML table".into()),
        };
        if let Some(v) = table.get("widen_delay") {
            cfg.widen_delay = v
                .as_integer()
                .ok_or_else(|| "widen_delay must be an integer".to_string())?
                .try_into()
                .map_err(|_| "widen_delay out of range".to_string())?;
        }
        if let Some(v) = table.get("narrow_iters") {
            cfg.narrow_iters = v
                .as_integer()
                .ok_or_else(|| "narrow_iters must be an integer".to_string())?
                .try_into()
                .map_err(|_| "narrow_iters out of range".to_string())?;
        }
        if let Some(v) = table.get("enable_narrowing") {
            cfg.enable_narrowing = v
                .as_bool()
                .ok_or_else(|| "enable_narrowing must be a boolean".to_string())?;
        }
        if let Some(v) = table.get("max_iterations") {
            cfg.max_iterations = v
                .as_integer()
                .ok_or_else(|| "max_iterations must be an integer".to_string())?
                .try_into()
                .map_err(|_| "max_iterations out of range".to_string())?;
        }
        if let Some(v) = table.get("concrete_fuel") {
            cfg.concrete_fuel = v
                .as_integer()
                .ok_or_else(|| "concrete_fuel must be an integer".to_string())?
                .try_into()
                .map_err(|_| "concrete_fuel out of range".to_string())?;
        }
        if cfg.widen_delay > cfg.max_iterations {
            return Err("widen_delay must not exceed max_iterations".into());
        }
        Ok(cfg)
    }

    pub fn merge_override(self, o: ConfigOverride) -> Config {
        Config {
            widen_delay: o.widen_delay.unwrap_or(self.widen_delay),
            narrow_iters: o.narrow_iters.unwrap_or(self.narrow_iters),
            enable_narrowing: o.enable_narrowing.unwrap_or(self.enable_narrowing),
            max_iterations: o.max_iterations.unwrap_or(self.max_iterations),
            concrete_fuel: o.concrete_fuel.unwrap_or(self.concrete_fuel),
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct ConfigOverride {
    pub widen_delay: Option<usize>,
    pub narrow_iters: Option<usize>,
    pub enable_narrowing: Option<bool>,
    pub max_iterations: Option<usize>,
    pub concrete_fuel: Option<usize>,
}
