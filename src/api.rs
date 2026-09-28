//! HTTP backend (Axum).
//!
//! Routes:
//! * `GET  /health`              — liveness + build info;
//! * `POST /api/v1/check`        — run an inclusion check;
//! * `POST /api/v1/verify-replay` — independently verify a replay against a
//!                                  model pair without running the solver.
//!
//! Error semantics (mirroring `ErrorKind`):
//! * 400 input_error         — malformed JSON / bad names / action outside alphabet;
//! * 409 state_conflict      — unknown states, silent declared observable, ...;
//! * 413 resource_exhausted  — request too large or a hard input limit hit
//!                             before solving (in-search exhaustion is instead
//!                             a 200 response with verdict `unknown`);
//! * 500 computation_failed  — internal invariant violation.

use axum::body::Bytes;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Serialize;
use serde_json::json;

use crate::engine;
use crate::error::EngineError;
use crate::input::CheckRequest;

#[derive(Debug, Serialize)]
struct ErrorBody {
    error: ErrorPayload,
}

#[derive(Debug, Serialize)]
struct ErrorPayload {
    kind: String,
    code: String,
    message: String,
}

impl IntoResponse for EngineError {
    fn into_response(self) -> Response {
        let status = self.kind.http_status();
        let body = ErrorBody {
            error: ErrorPayload {
                kind: self.kind.as_str().to_string(),
                code: self.code,
                message: self.message,
            },
        };
        (status, Json(body)).into_response()
    }
}

fn bad_request(code: &str, message: String) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(ErrorBody {
            error: ErrorPayload {
                kind: "input_error".to_string(),
                code: code.to_string(),
                message,
            },
        }),
    )
        .into_response()
}

async fn health() -> impl IntoResponse {
    Json(json!({
        "status": "ok",
        "service": "wtio",
        "version": env!("CARGO_PKG_VERSION"),
    }))
}

async fn check(body: Bytes) -> Response {
    let request: CheckRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(e) => return bad_request("invalid_json", format!("request body is not valid JSON: {e}")),
    };
    match engine::run_check(&request) {
        Ok(resp) => (StatusCode::OK, Json(resp)).into_response(),
        Err(e) => e.into_response(),
    }
}

#[derive(serde::Deserialize)]
struct VerifyReplayRequest {
    model: CheckRequest,
    trace: Vec<String>,
    replay: crate::witness::Replay,
}

async fn verify_replay(body: Bytes) -> Response {
    let req: VerifyReplayRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(e) => return bad_request("invalid_json", format!("request body is not valid JSON: {e}")),
    };
    let result = (|| {
        let pair = crate::compiler::compile(&req.model)?;
        crate::verifier::verify_counterexample(&pair, &req.replay, &req.trace)
    })();
    match result {
        Ok(v) => (StatusCode::OK, Json(v)).into_response(),
        Err(e) => e.into_response(),
    }
}

pub fn app() -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/api/v1/check", post(check))
        .route("/api/v1/verify-replay", post(verify_replay))
        .fallback(|| async {
            (
                StatusCode::NOT_FOUND,
                Json(ErrorBody {
                    error: ErrorPayload {
                        kind: "input_error".into(),
                        code: "not_found".into(),
                        message: "unknown route; use POST /api/v1/check".into(),
                    },
                }),
            )
        })
}

/// Run the server (used by the binary).
pub async fn serve(addr: std::net::SocketAddr) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind(addr).await?;
    let bound = listener.local_addr()?;
    tracing::info!("wtio listening on http://{bound}");
    println!("wtio listening on http://{bound}");
    axum::serve(listener, app())
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("shutdown signal received");
        })
        .await
}
