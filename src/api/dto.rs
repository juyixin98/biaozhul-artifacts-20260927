//! HTTP request/response types.

use serde::{Deserialize, Serialize};

use crate::config::EngineConfig;
use crate::evidence::concrete::ConcreteInput;
use crate::kernel::report::AnalysisReport;
use crate::lang::ProgramJson;

/// One program submission: either `source` text or a structured `json` body.
#[derive(Debug, Deserialize)]
pub struct AnalyzeRequest {
    /// Program in the textual DSL.
    #[serde(default)]
    pub source: Option<String>,
    /// Program as JSON envelope (`params` + `body` lines).
    #[serde(default)]
    pub json: Option<ProgramJson>,
    /// Optional per-request engine overrides (clamped to sane bounds).
    #[serde(default)]
    pub engine: Option<EngineOverrides>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct EngineOverrides {
    #[serde(default)]
    pub max_paths: Option<u32>,
    #[serde(default)]
    pub loop_unroll: Option<u32>,
    #[serde(default)]
    pub solver_timeout_ms: Option<u32>,
}

impl EngineOverrides {
    /// Apply overrides onto a base config with hard sanity caps.
    pub fn apply(&self, base: &EngineConfig) -> EngineConfig {
        let mut cfg = base.clone();
        if let Some(v) = self.max_paths {
            cfg.max_paths = v.clamp(1, 100_000);
        }
        if let Some(v) = self.loop_unroll {
            cfg.loop_unroll = v.clamp(1, 1_000);
        }
        if let Some(v) = self.solver_timeout_ms {
            cfg.solver_timeout_ms = v.clamp(1, 60_000);
        }
        cfg
    }
}

#[derive(Debug, Serialize)]
pub struct AnalyzeResponse {
    #[serde(flatten)]
    pub report: AnalysisReport,
}

/// Error body; `code` is stable for clients, `message` is for humans.
#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub error: ErrorDetail,
}

#[derive(Debug, Serialize)]
pub struct ErrorDetail {
    pub code: &'static str,
    pub message: String,
    pub run_id: Option<String>,
}

impl ErrorBody {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        ErrorBody {
            error: ErrorDetail {
                code,
                message: message.into(),
                run_id: None,
            },
        }
    }
    pub fn with_run(code: &'static str, message: impl Into<String>, run_id: String) -> Self {
        ErrorBody {
            error: ErrorDetail {
                code,
                message: message.into(),
                run_id: Some(run_id),
            },
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct ReplayRequest {
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub json: Option<ProgramJson>,
    pub input: ConcreteInput,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub service: &'static str,
    pub version: &'static str,
    pub smt_backend: &'static str,
    pub smt_version: String,
}
