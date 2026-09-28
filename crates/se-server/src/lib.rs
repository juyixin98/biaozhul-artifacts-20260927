//! # se-server
//!
//! Axum HTTP backend and CLI front-end for the bounded symbolic-execution service.
//!
//! Layers:
//! * [`config`] — file/env/CLI configuration with pinned defaults.
//! * [`api`] — typed JSON endpoints: analyze (engine + independent replay), direct
//!   concrete replay, bounded exhaustive oracle, health/version.

pub mod api;
pub mod config;

use axum::routing::{get, post};
use axum::Router;

use api::AppState;

use tower_http_lite::error_mapping_layer;

/// Build the application router. `body_limit` is enforced manually (see
/// `tower_http_lite`) to avoid depending on a large middleware crate.
pub fn app(state: AppState, body_limit: usize) -> Router {
    Router::new()
        .route("/health", get(api::health))
        .route("/version", get(api::version))
        .route("/analyze", post(api::analyze))
        .route("/verify/replay", post(api::replay))
        .route("/oracle", post(api::oracle))
        .layer(axum::middleware::from_fn(move |req, next| {
            error_mapping_layer(req, next, body_limit)
        }))
        .with_state(state)
}

/// Minimal local middleware: reject oversized JSON bodies and require
/// `application/json` on POST routes, turning extractor failures into structured API
/// errors instead of opaque 500s.
mod tower_http_lite {
    use axum::body::{to_bytes, Body};
    use axum::extract::Request;
    use axum::http::StatusCode;
    use axum::middleware::Next;
    use axum::response::Response;
    use axum::Json;
    use serde_json::json;

    pub async fn error_mapping_layer(
        req: Request,
        next: Next,
        limit: usize,
    ) -> Response {
        // Reject based on declared content length first (cheap, no buffering).
        if let Some(len) = req.headers().get(axum::http::header::CONTENT_LENGTH) {
            if let Ok(txt) = len.to_str() {
                if let Ok(n) = txt.parse::<usize>() {
                    if n > limit {
                        return api_error(
                            StatusCode::PAYLOAD_TOO_LARGE,
                            "payload_too_large",
                            format!("content-length {n} exceeds limit {limit}"),
                        );
                    }
                }
            }
        }

        // Require JSON content type when one is given.
        if let Some(ct) = req.headers().get(axum::http::header::CONTENT_TYPE) {
            if let Ok(txt) = ct.to_str() {
                if !txt.is_empty()
                    && !txt.to_ascii_lowercase().contains("application/json")
                {
                    return api_error(
                        StatusCode::UNSUPPORTED_MEDIA_TYPE,
                        "unsupported_media_type",
                        "Content-Type must be application/json".to_string(),
                    );
                }
            }
        }

        // Buffer with a hard cap to defend against chunked requests lying about
        // content-length.
        let (parts, body) = req.into_parts();
        let bytes = match to_bytes(body, limit + 1).await {
            Ok(b) if b.len() <= limit => b,
            Ok(_) => {
                return api_error(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "payload_too_large",
                    format!("body exceeds limit of {limit} bytes"),
                );
            }
            Err(_) => {
                return api_error(
                    StatusCode::PAYLOAD_TOO_LARGE,
                    "payload_too_large",
                    format!("body exceeds limit of {limit} bytes"),
                );
            }
        };

        let req = Request::from_parts(parts, Body::from(bytes));
        let resp = next.run(req).await;

        // Map JSON body extractor failures (422) to structured 400 responses.
        if resp.status() == StatusCode::UNPROCESSABLE_ENTITY {
            return api_error(
                StatusCode::BAD_REQUEST,
                "bad_request",
                "request body could not be parsed as the expected JSON schema".to_string(),
            );
        }
        resp
    }

    fn api_error(status: StatusCode, code: &str, message: String) -> Response {
        use axum::response::IntoResponse;
        (
            status,
            Json(json!({ "error": code, "message": message })),
        )
            .into_response()
    }
}
