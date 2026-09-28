//! HTTP API types and request handling.
//!
//! Endpoints (all JSON, all local-only by default):
//!
//! * `GET  /health`        — liveness + backend availability/version.
//! * `GET  /version`       — component versions and engine constants.
//! * `POST /analyze`       — symbolic analysis + independent evidence replay.
//! * `POST /verify/replay` — replay a supplied input assignment against a program.
//! * `POST /oracle`        — small-domain exhaustive ground truth (bounded).
//!
//! Error responses use HTTP 4xx/5xx with a structured body; an analysis that cannot be
//! concluded returns HTTP 200 with verdict `"unknown"` — unknown is never remapped to
//! success.

use std::sync::Arc;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::{Deserialize, Serialize};
use serde_json::json;

use se_engine::{Engine, EngineConfig};
use se_lang::dto::ProgramDto;
use se_lang::interp::{self, RunOpts};
use se_lang::util::content_id;
use se_solver::{SmtSolver, Z3Cli};
use se_verify::oracle::exhaustive_oracle;
use se_verify::{verify_report, VerifiedReport};

use crate::config::AppConfig;

#[derive(Clone)]
pub struct AppState {
    pub cfg: Arc<AppConfig>,
    pub solver: Arc<Z3Cli>,
}

impl AppState {
    pub fn new(cfg: AppConfig) -> Self {
        let solver = Arc::new(Z3Cli::new(cfg.solver.bin.clone(), cfg.solver.timeout_ms));
        AppState {
            cfg: Arc::new(cfg),
            solver,
        }
    }

    fn engine_config(&self, req: &AnalysisRequest) -> EngineConfig {
        let d = &self.cfg.engine;
        EngineConfig {
            max_paths: req.max_paths.unwrap_or(d.max_paths),
            max_loop_unroll: req.max_loop_unroll.unwrap_or(d.max_loop_unroll),
            enforce_domains: req.enforce_domains.unwrap_or(d.enforce_domains),
            record_limit: d.record_limit,
        }
    }

    fn replay_opts(&self) -> RunOpts {
        RunOpts {
            step_limit: self.cfg.verify.replay_step_limit,
            record_trace: true,
        }
    }
}

// ---------------------------------------------------------------------------
// Request/response DTOs
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, Deserialize)]
pub struct AnalysisRequest {
    /// Either a raw JSON program object or its canonical string form.
    pub program: serde_json::Value,
    #[serde(default)]
    pub max_paths: Option<usize>,
    #[serde(default)]
    pub max_loop_unroll: Option<u32>,
    #[serde(default)]
    pub enforce_domains: Option<bool>,
    /// Run the bounded exhaustive oracle alongside and include it in the response.
    #[serde(default)]
    pub with_oracle: bool,
    /// Override the oracle assignment cap for this request.
    #[serde(default)]
    pub oracle_cap: Option<u64>,
    /// Caller-supplied correlation id; echoed back. Generated if absent.
    #[serde(default)]
    pub request_id: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AnalysisResponse {
    pub request_id: String,
    pub run_id: String,
    pub program_id: String,
    pub verdict: String,
    pub replay: VerifiedReport,
    pub report: se_engine::AnalysisReport,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oracle: Option<se_verify::OracleSummary>,
    pub backend: BackendInfo,
}

#[derive(Clone, Debug, Serialize)]
pub struct BackendInfo {
    pub solver: String,
    pub solver_version: String,
    pub solver_available: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ReplayRequest {
    pub program: serde_json::Value,
    /// Input name -> unsigned w-bit value.
    pub inputs: std::collections::BTreeMap<String, u64>,
    #[serde(default)]
    pub request_id: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ReplayResponse {
    pub request_id: String,
    pub run_id: String,
    pub outcome: String,
    pub failure_kind: Option<String>,
    pub failure_stmt: Option<usize>,
    pub failure_op: Option<String>,
    pub steps: u64,
    pub trace: Vec<usize>,
    pub final_store: std::collections::BTreeMap<String, u64>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct OracleRequest {
    pub program: serde_json::Value,
    #[serde(default)]
    pub cap: Option<u64>,
    #[serde(default)]
    pub request_id: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
    pub solver: String,
    pub solver_version: String,
    pub solver_available: bool,
}

/// Uniform API error body.
#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
    pub request_id: Option<String>,
}

impl ApiError {
    pub fn bad_request(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::BAD_REQUEST,
            code: "bad_request",
            message: message.into(),
            request_id: None,
        }
    }
    pub fn payload_too_large(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::PAYLOAD_TOO_LARGE,
            code: "payload_too_large",
            message: message.into(),
            request_id: None,
        }
    }
    pub fn unsupported_media(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::UNSUPPORTED_MEDIA_TYPE,
            code: "unsupported_media_type",
            message: message.into(),
            request_id: None,
        }
    }
    pub fn internal(message: impl Into<String>) -> Self {
        ApiError {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal",
            message: message.into(),
            request_id: None,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = Json(json!({
            "error": self.code,
            "message": self.message,
            "request_id": self.request_id,
        }));
        (self.status, body).into_response()
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn parse_program(value: &serde_json::Value) -> Result<(se_lang::Program, String, String), ApiError> {
    let canonical = match value {
        serde_json::Value::String(s) => s.clone(),
        other => serde_json::to_string(other)
            .map_err(|e| ApiError::bad_request(format!("program must be JSON: {e}")))?,
    };
    match ProgramDto::parse(&canonical) {
        Ok((program, meta)) => Ok((program, canonical, meta.canonical_json)),
        Err(e) => Err(ApiError::bad_request(e.to_string())),
    }
}

fn new_run_id() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("r-{nanos:016x}")
}

fn backend_info(state: &AppState) -> BackendInfo {
    BackendInfo {
        solver: state.solver.name().to_string(),
        solver_version: state.solver.version().to_string(),
        solver_available: state.solver.available(),
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

pub async fn health(State(state): State<AppState>) -> impl IntoResponse {
    Json(HealthResponse {
        status: "ok",
        solver: state.solver.name().to_string(),
        solver_version: state.solver.version().to_string(),
        solver_available: state.solver.available(),
    })
}

pub async fn version() -> impl IntoResponse {
    Json(json!({
        "service": env!("CARGO_PKG_NAME"),
        "version": env!("CARGO_PKG_VERSION"),
        "engine": se_engine::ENGINE_VERSION,
        "language": "se-lang 0.1.0",
        "solver_interface": "SMT-LIB 2 / QF_BV",
    }))
}

pub async fn analyze(
    State(state): State<AppState>,
    Json(req): Json<AnalysisRequest>,
) -> Result<Json<AnalysisResponse>, ApiError> {
    let request_id = req
        .request_id
        .clone()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(new_run_id);
    let run_id = new_run_id();

    let (program, _raw, canonical) = parse_program(&req.program)?;
    let program_id = content_id(&canonical);

    tracing::info!(
        request_id = %request_id,
        run_id = %run_id,
        program_id = %program_id,
        solver = %state.solver.name(),
        solver_version = %state.solver.version(),
        "analyze start"
    );

    if !state.solver.available() {
        return Err(ApiError::internal(format!(
            "solver backend '{}' is not available (install z3 or set SE_Z3_BIN)",
            state.cfg.solver.bin
        )));
    }

    let engine_cfg = state.engine_config(&req);
    let report = Engine::new(&program, state.solver.as_ref(), engine_cfg).analyze();

    let replay = verify_report(&program, &report, state.replay_opts());

    // Final verdict = post-replay verdict. Never let an engine violation survive
    // without a confirmed witness.
    let verdict = replay.final_verdict.clone();

    tracing::info!(
        request_id = %request_id,
        run_id = %run_id,
        engine_verdict = %report.verdict,
        final_verdict = %verdict,
        confirmed = replay.confirmed_count,
        rejected = replay.rejected_count,
        cuts = report.cuts.len(),
        "analyze complete"
    );

    let oracle = if req.with_oracle {
        let cap = req.oracle_cap.unwrap_or(state.cfg.verify.oracle_cap);
        Some(exhaustive_oracle(
            &program,
            cap,
            RunOpts {
                step_limit: state.cfg.verify.replay_step_limit,
                record_trace: false,
            },
        ))
    } else {
        None
    };

    Ok(Json(AnalysisResponse {
        request_id,
        run_id,
        program_id,
        verdict,
        replay,
        report,
        oracle,
        backend: backend_info(&state),
    }))
}

pub async fn replay(
    State(state): State<AppState>,
    Json(req): Json<ReplayRequest>,
) -> Result<Json<ReplayResponse>, ApiError> {
    let request_id = req
        .request_id
        .clone()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(new_run_id);
    let run_id = new_run_id();
    let (program, _raw, _canonical) = parse_program(&req.program)?;

    let result = interp::run(
        &program,
        &req.inputs,
        RunOpts {
            step_limit: state.cfg.verify.replay_step_limit,
            record_trace: true,
        },
    );

    tracing::info!(
        request_id = %request_id,
        run_id = %run_id,
        outcome = ?result.outcome,
        steps = result.steps,
        "replay"
    );

    let (outcome, kind, stmt, op) = match result.outcome {
        interp::FlowOutcome::Completed => ("completed".to_string(), None, None, None),
        interp::FlowOutcome::InfeasibleAssume => {
            ("infeasible_assume".to_string(), None, None, None)
        }
        interp::FlowOutcome::Failed(f) => (
            "failed".to_string(),
            Some(f.kind.as_str().to_string()),
            Some(f.stmt_id),
            f.op.map(|s| s.to_string()),
        ),
    };

    Ok(Json(ReplayResponse {
        request_id,
        run_id,
        outcome,
        failure_kind: kind,
        failure_stmt: stmt,
        failure_op: op,
        steps: result.steps,
        trace: result.trace,
        final_store: result.final_store,
    }))
}

pub async fn oracle(
    State(state): State<AppState>,
    Json(req): Json<OracleRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let request_id = req
        .request_id
        .clone()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(new_run_id);
    let run_id = new_run_id();
    let (program, _raw, canonical) = parse_program(&req.program)?;
    let cap = req.cap.unwrap_or(state.cfg.verify.oracle_cap);

    tracing::info!(request_id = %request_id, run_id = %run_id, cap = cap, "oracle start");
    let summary = exhaustive_oracle(
        &program,
        cap,
        RunOpts {
            step_limit: state.cfg.verify.replay_step_limit,
            record_trace: false,
        },
    );
    tracing::info!(
        request_id = %request_id,
        run_id = %run_id,
        verdict = %summary.verdict,
        assignments = summary.total_assignments,
        failures = summary.failures.len(),
        truncated = summary.truncated,
        "oracle complete"
    );

    Ok(Json(json!({
        "request_id": request_id,
        "run_id": run_id,
        "program_id": content_id(&canonical),
        "summary": summary,
    })))
}
