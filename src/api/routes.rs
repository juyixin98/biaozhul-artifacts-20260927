//! Axum routes and run orchestration.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};

use crate::api::dto::*;
use crate::config::{ConfigFile, EngineConfig};
use crate::evidence::concrete;
use crate::kernel;
use crate::lang;

#[derive(Clone)]
pub struct AppState {
    pub engine: EngineConfig,
    pub run_counter: Arc<AtomicU64>,
}

pub fn build_router(cfg: ConfigFile) -> Router {
    let state = AppState {
        engine: cfg.engine,
        run_counter: Arc::new(AtomicU64::new(0)),
    };
    Router::new()
        .route("/v1/health", get(health))
        .route("/v1/analyze", post(analyze))
        .route("/v1/replay", post(replay))
        .with_state(state)
}

fn next_run_id(state: &AppState) -> String {
    let n = state.run_counter.fetch_add(1, Ordering::Relaxed);
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("run-{millis:x}-{n}")
}

fn parse_program(req_source: Option<&str>, req_json: Option<&lang::ProgramJson>) -> Result<lang::Program, (StatusCode, ErrorBody)> {
    match (req_source, req_json) {
        (Some(_), Some(_)) => Err((
            StatusCode::BAD_REQUEST,
            ErrorBody::new("invalid_request", "provide either `source` or `json`, not both"),
        )),
        (None, None) => Err((
            StatusCode::BAD_REQUEST,
            ErrorBody::new("invalid_request", "provide `source` or `json`"),
        )),
        (Some(src), None) => lang::program_from_source(src).map_err(|e| {
            (
                StatusCode::BAD_REQUEST,
                ErrorBody::new("invalid_program", e.to_string()),
            )
        }),
        (None, Some(j)) => lang::program_from_json(j).map_err(|e| {
            (
                StatusCode::BAD_REQUEST,
                ErrorBody::new("invalid_program", e.to_string()),
            )
        }),
    }
}

async fn health(State(state): State<AppState>) -> impl IntoResponse {
    let _ = state;
    Json(HealthResponse {
        status: "ok",
        service: crate::SERVICE_NAME,
        version: crate::SERVICE_VERSION,
        smt_backend: "z3",
        smt_version: crate::z3_version(),
    })
}

async fn analyze(
    State(state): State<AppState>,
    Json(req): Json<AnalyzeRequest>,
) -> Result<Json<AnalyzeResponse>, (StatusCode, Json<ErrorBody>)> {
    let run_id = next_run_id(&state);
    let program = parse_program(req.source.as_deref(), req.json.as_ref())
        .map_err(|(s, b)| (s, Json(b)))?;
    let cfg = req
        .engine
        .as_ref()
        .map(|o| o.apply(&state.engine))
        .unwrap_or_else(|| state.engine.clone());

    tracing::info!(
        run_id = %run_id,
        params = program.params.len(),
        stmts = program.body.len(),
        max_paths = cfg.max_paths,
        loop_unroll = cfg.loop_unroll,
        "analysis started"
    );

    let run_id2 = run_id.clone();
    let result = tokio::task::spawn_blocking(move || kernel::analyze(program, cfg, run_id2))
        .await
        .map_err(|e| {
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorBody::with_run(
                    "engine_panic",
                    format!("analysis task failed: {e}"),
                    run_id.clone(),
                )),
            )
        })?;

    match result {
        Ok(report) => Ok(Json(AnalyzeResponse { report })),
        Err(mismatch) => {
            tracing::error!(
                run_id = %run_id,
                detail = %mismatch,
                "replay mismatch: solver/replay disagreement"
            );
            Err((
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(ErrorBody::with_run(
                    "internal_inconsistency",
                    mismatch.to_string(),
                    run_id,
                )),
            ))
        }
    }
}

async fn replay(
    State(state): State<AppState>,
    Json(req): Json<ReplayRequest>,
) -> Result<Json<concrete::ConcreteResult>, (StatusCode, Json<ErrorBody>)> {
    let _ = state;
    let program = parse_program(req.source.as_deref(), req.json.as_ref())
        .map_err(|(s, b)| (s, Json(b)))?;
    match concrete::run(&program, &req.input) {
        Ok(r) => Ok(Json(r)),
        Err(e) => Err((
            StatusCode::BAD_REQUEST,
            Json(ErrorBody::new("invalid_input", e.to_string())),
        )),
    }
}
