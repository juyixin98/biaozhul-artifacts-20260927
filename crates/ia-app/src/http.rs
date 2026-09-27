//! Axum HTTP routes: `GET /healthz`, `POST /api/analyze`, `POST /api/verify`.
use crate::dto::{AnalyzeRequest, VerifyRequest};
use crate::service::{run_analyze, run_verify, SERVICE_NAME};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use serde_json::json;

pub fn router() -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/api/analyze", axum::routing::post(analyze_route))
        .route("/api/verify", axum::routing::post(verify_route))
}

async fn healthz() -> Response {
    (
        StatusCode::OK,
        Json(json!({
            "status": "ok",
            "service": SERVICE_NAME,
            "service_version": env!("CARGO_PKG_VERSION"),
            "lang_version": ia_lang::VERSION,
            "solver_version": ia_solver::SOLVER_VERSION,
        })),
    )
        .into_response()
}

async fn analyze_route(Json(req): Json<AnalyzeRequest>) -> Response {
    let envelope = run_analyze(req);
    let status = if envelope.ok {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    (status, Json(envelope)).into_response()
}

async fn verify_route(Json(req): Json<VerifyRequest>) -> Response {
    let envelope = run_verify(req);
    let status = if envelope.ok {
        StatusCode::OK
    } else {
        StatusCode::UNPROCESSABLE_ENTITY
    };
    (status, Json(envelope)).into_response()
}
