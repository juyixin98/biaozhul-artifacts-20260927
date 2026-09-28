//! HTTP routing and request handling.

use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use tracing::{debug, info, warn};

use pn_lang::{parse_analyze_request, InputErrorCategory};
use pn_solver::analyze_with_config;

use crate::config::Config;
use crate::dto::{error_dto, success_dto, ErrorDetailDto};
use crate::runid::RunId;

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/version", get(version))
        .route("/api/v1/analyze", post(analyze))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({"status": "up"}))
}

async fn version(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "service": "petri-reachability-backend",
        "version": state.config.server_version,
        "schema": "petri-analysis/v1",
        "model": "bounded-capacity ordinary weighted Petri net",
        "unbounded_reachability_complete": false,
    }))
}

async fn analyze(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let hint = headers
        .get("x-run-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());
    let run_id = RunId::generate(hint);
    let rid = run_id.as_str();
    let version = state.config.server_version.clone();
    let bytes_len = body.len();

    info!(run_id = rid, bytes = bytes_len, "analyze request received");

    // Parse + classify input failures.
    let input = match parse_analyze_request(&body) {
        Ok(input) => input,
        Err(report) => {
            let category = report.primary_category();
            let cat = category.as_str();
            let details: Vec<ErrorDetailDto> = report
                .errors
                .iter()
                .map(|e| ErrorDetailDto {
                    category: e.category.as_str().to_string(),
                    message: e.message.clone(),
                    pointer: e.pointer.clone(),
                })
                .collect();
            warn!(
                run_id = rid,
                category = cat,
                count = details.len(),
                "input rejected"
            );
            let status = match category {
                InputErrorCategory::Syntax | InputErrorCategory::Schema => {
                    StatusCode::BAD_REQUEST
                }
                InputErrorCategory::Semantic => StatusCode::UNPROCESSABLE_ENTITY,
            };
            let body = error_dto(
                rid,
                &version,
                cat,
                "request failed input validation".to_string(),
                details,
            );
            return (status, Json(body)).into_response();
        }
    };

    debug!(
        run_id = rid,
        net = %input.name,
        places = input.net.place_count(),
        transitions = input.net.transition_count(),
        targets = input.targets.len(),
        "input validated"
    );

    // Clamp the client's state budget to the server ceiling so a request can
    // never make the server exhaust itself; an explicit clamp is logged.
    let ceiling = state.config.max_states_ceiling;
    let effective_budget = match input.options.max_states {
        Some(n) => Some(n.min(ceiling)),
        None => Some(ceiling),
    };
    if input.options.max_states.is_some_and(|n| n > ceiling) {
        warn!(
            run_id = rid,
            requested = input.options.max_states,
            ceiling,
            "client max_states exceeded server ceiling; clamped"
        );
    }

    // Run the analysis. The solver performs bounded exhaustive search; the DTO
    // layer independently re-verifies every witness and deadlock.
    let outcome = analyze_with_config(&input, effective_budget);

    info!(
        run_id = rid,
        targets = outcome.targets.len(),
        total_states_expanded = outcome.total_states_expanded,
        deadlocks = outcome.deadlocks.len(),
        deadlocks_truncated = outcome.deadlocks_truncated,
        invariants = outcome.invariants.len(),
        truncated = outcome.invariant_generation_truncated,
        "analysis complete"
    );

    let api = success_dto(&outcome, &input.net, rid, &version);
    (StatusCode::OK, Json(api)).into_response()
}
