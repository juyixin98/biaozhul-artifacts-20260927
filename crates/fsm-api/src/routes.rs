//! Axum application: routes and handlers.

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use serde_json::json;
use std::sync::Arc;
use tracing::info;

use crate::middleware::RequestId;
use crate::service::ServiceState;
use crate::types::{prepare, CheckRequest};

pub fn app(state: Arc<ServiceState>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/api/v1/version", get(version))
        .route("/api/v1/check", post(check))
        .route("/api/v1/verify", post(verify))
        .route("/api/v1/fixtures", get(list_fixtures))
        .layer(axum::middleware::from_fn(crate::middleware::layer))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({"status": "ok"}))
}

async fn version() -> Json<serde_json::Value> {
    Json(json!({
        "service": "explicit-fsm-checker",
        "api": "v1",
        "versions": {
            "fsm-lang": fsm_lang::VERSION,
            "fsm-core": fsm_core::VERSION,
            "fsm-verify": env!("CARGO_PKG_VERSION"),
        },
        "engine": "budgeted breadth-first explicit-state search; AG/EF subset",
        "codec": "bijective mixed-radix u64",
    }))
}

async fn list_fixtures() -> Json<serde_json::Value> {
    Json(json!({
        "fixtures": [
            "mutex_safe", "mutex_bad", "counter", "counter_deadlock",
            "no_init", "big_counter", "swap"
        ]
    }))
}

/// Structured error envelope: failure reasons live in their own field,
/// separate from uncertain (`unknown`) conclusions which are regular
/// property results.
struct ApiError {
    status: StatusCode,
    category: String,
    detail: String,
    position: Option<(usize, usize)>,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let body = json!({
            "ok": false,
            "error": {
                "category": self.category,
                "detail": self.detail,
                "position": self.position,
            }
        });
        (self.status, Json(body)).into_response()
    }
}

async fn check(
    State(svc): State<Arc<ServiceState>>,
    Extension(rid): Extension<RequestId>,
    Json(req): Json<CheckRequest>,
) -> Result<Response, ApiError> {
    info!(request_id = %rid.0, kind = "check", "received check request");
    let resolved = prepare(req, svc.config.default_max_states).map_err(|f| ApiError {
        status: StatusCode::BAD_REQUEST,
        category: f.category,
        detail: f.detail,
        position: f.position,
    })?;
    let outcome = fsm_core::run_check(&resolved.system, &resolved.queries, resolved.options);

    let mut body = serde_json::to_value(&outcome).expect("outcome serializes");
    body.as_object_mut()
        .unwrap()
        .insert("ok".into(), json!(true));
    body.as_object_mut()
        .unwrap()
        .insert("request_id".into(), json!(rid.0));
    body.as_object_mut().unwrap().insert(
        "processing".into(),
        json!({
            "location": "fsm-core::run_check",
            "budget": resolved.options.max_states,
            "state_space_product": resolved.system.total_space(),
        }),
    );

    // Log uncertain conclusions separately and explicitly.
    for p in &outcome.properties {
        if matches!(p.conclusion, fsm_core::Conclusion::Unknown) {
            info!(
                request_id = %rid.0,
                property = %p.name,
                reason = ?p.reason,
                "UNCERTAIN conclusion (budget truncated)"
            );
        }
    }
    Ok(Json(body).into_response())
}

#[derive(serde::Deserialize)]
struct VerifyBody {
    #[serde(default)]
    fixture: Option<String>,
    #[serde(default)]
    spec_text: Option<String>,
    #[serde(default)]
    spec_json: Option<serde_json::Value>,
    evidence: fsm_verify::EvidenceInput,
}

async fn verify(
    Extension(rid): Extension<RequestId>,
    Json(body): Json<VerifyBody>,
) -> Result<Response, ApiError> {
    info!(request_id = %rid.0, kind = "verify", "received evidence verification request");
    let check_req = CheckRequest {
        fixture: body.fixture,
        spec_text: body.spec_text,
        spec_json: body.spec_json,
        properties: vec![],
        max_states: None,
        check_deadlock: Some(false),
    };
    let raw = crate::types::resolve_spec(&check_req).map_err(|f| ApiError {
        status: StatusCode::BAD_REQUEST,
        category: f.category,
        detail: f.detail,
        position: f.position,
    })?;
    let system = fsm_lang::eval::build_system(raw)
        .map_err(crate::types::build_failure)
        .map_err(|f| ApiError {
            status: StatusCode::BAD_REQUEST,
            category: f.category,
            detail: f.detail,
            position: f.position,
        })?;
    let report = fsm_verify::verify(&system, &body.evidence);
    let status = if report.accepted {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    let resp = json!({
        "ok": report.accepted,
        "request_id": rid.0,
        "processing": {"location": "fsm-verify::verify (independent replay)"},
        "report": report,
    });
    Ok((status, Json(resp)).into_response())
}
