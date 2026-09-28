//! HTTP layer (axum): routing, middleware, handlers.
//!
//! The wire contract is explicit: every mutating endpoint takes raw
//! [`Bytes`], parses into a typed DTO via [`parse_json`], and returns
//! `Result<Json<T>, ApiError>`. Parsing failures (invalid JSON, wrong shape,
//! unknown field) and oversize bodies are mapped onto the crate's
//! [`ServiceError`] classes, so the documented error kinds are the *only*
//! error shape clients ever see.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Bytes;
use axum::extract::DefaultBodyLimit;
use axum::http::{HeaderValue, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use tower::ServiceBuilder;
use tracing::{info, warn};

use crate::error::ServiceError;
use crate::service::ConstraintService;
use crate::store::BatchOp;

pub mod dto;

/// Request bodies larger than this are rejected as `resource_exhausted`.
pub const MAX_BODY_BYTES: usize = 1 << 20; // 1 MiB

/// Per-request id stored in request extensions by the middleware (available
/// to deeper layers that take `Extension<RequestId>`); the middleware also
/// echoes it in the `x-request-id` response header.
#[derive(Clone)]
#[allow(dead_code)]
struct RequestId(String);

#[derive(Clone)]
pub struct AppState {
    pub service: Arc<ConstraintService>,
}

/// Error wrapper implementing [`IntoResponse`] with the documented status map.
#[derive(Debug)]
pub struct ApiError(pub ServiceError);

impl From<ServiceError> for ApiError {
    fn from(e: ServiceError) -> Self {
        ApiError(e)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = StatusCode::from_u16(self.0.kind.http_status())
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        let body = serde_json::json!({
            "error": {
                "kind": self.0.kind,
                "message": self.0.message,
                "detail": self.0.detail,
            }
        });
        warn!(kind = self.0.kind.as_str(), "request failed");
        (status, Json(body)).into_response()
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok" }))
}

#[derive(serde::Serialize)]
struct ListResponse {
    revision: u64,
    count: usize,
    constraints: Vec<crate::model::Constraint>,
}

async fn list_constraints(
    axum::extract::State(st): axum::extract::State<AppState>,
) -> Json<ListResponse> {
    let revision = st.service.revision();
    let constraints = st.service.list();
    let count = constraints.len();
    Json(ListResponse {
        revision,
        count,
        constraints,
    })
}

async fn add_constraints(
    axum::extract::State(st): axum::extract::State<AppState>,
    body: Bytes,
) -> Result<Json<serde_json::Value>, ApiError> {
    let req: dto::AddRequest = parse_json(&body)?;
    let mut ops = Vec::new();
    for item in req.constraints {
        ops.push(BatchOp::Add(item.into_constraint()?));
    }
    if let Some(text) = req.text {
        for c in crate::lang::parse_constraints(&text)? {
            ops.push(BatchOp::Add(c));
        }
    }
    if ops.is_empty() {
        return Err(ServiceError::input("no constraints supplied").into());
    }
    let results = st.service.batch(ops)?;
    Ok(Json(serde_json::json!({
        "applied": results,
        "revision": st.service.revision(),
    })))
}

async fn replace_all(
    axum::extract::State(st): axum::extract::State<AppState>,
    body: Bytes,
) -> Result<Json<serde_json::Value>, ApiError> {
    let req: dto::ReplaceRequest = parse_json(&body)?;
    // Atomic replace: clear then add, all in one batch. A duplicate id inside
    // the request (or a parse error in the text block) aborts the whole
    // replace and leaves the previous set intact.
    let mut ops = vec![BatchOp::Clear];
    let mut seen = std::collections::BTreeSet::new();
    for item in req.constraints {
        let c = item.into_constraint()?;
        if !seen.insert(c.id.clone()) {
            return Err(ServiceError::state_conflict(format!(
                "duplicate constraint id '{}' in replace request",
                c.id
            ))
            .into());
        }
        ops.push(BatchOp::Add(c));
    }
    if let Some(text) = req.text {
        for c in crate::lang::parse_constraints(&text)? {
            if !seen.insert(c.id.clone()) {
                return Err(ServiceError::state_conflict(format!(
                    "duplicate constraint id '{}' in replace request text",
                    c.id
                ))
                .into());
            }
            ops.push(BatchOp::Add(c));
        }
    }
    let results = st.service.batch(ops)?;
    Ok(Json(serde_json::json!({
        "applied": results,
        "revision": st.service.revision(),
    })))
}

async fn update_one(
    axum::extract::State(st): axum::extract::State<AppState>,
    body: Bytes,
) -> Result<Json<serde_json::Value>, ApiError> {
    let req: dto::UpdateRequest = parse_json(&body)?;
    let c = req.constraint.into_constraint()?;
    let result = st.service.update(c)?;
    Ok(Json(serde_json::json!({
        "applied": result,
        "revision": st.service.revision(),
    })))
}

async fn delete_one(
    axum::extract::State(st): axum::extract::State<AppState>,
    body: Bytes,
) -> Result<Json<serde_json::Value>, ApiError> {
    let req: dto::DeleteRequest = parse_json(&body)?;
    let result = st.service.delete(&req.id)?;
    Ok(Json(serde_json::json!({
        "applied": result,
        "revision": st.service.revision(),
    })))
}

async fn solve(
    axum::extract::State(st): axum::extract::State<AppState>,
    body: Bytes,
) -> Result<Json<dto::SolveResponse>, ApiError> {
    let req: Option<dto::SolveRequest> = if body.is_empty() {
        None
    } else {
        Some(parse_json(&body)?)
    };
    let ids = req.and_then(|r| r.only_ids);
    let answer = st.service.solve(ids.as_deref())?;
    Ok(Json(dto::SolveResponse::from_answer(answer)))
}

async fn verify(
    axum::extract::State(st): axum::extract::State<AppState>,
    body: Bytes,
) -> Result<Json<dto::VerifyResponse>, ApiError> {
    let req: dto::VerifyRequest = parse_json(&body)?;
    if let Some(assignment) = req.assignment {
        let check = st
            .service
            .verify_assignment(&assignment, req.only_ids.as_deref())?;
        return Ok(Json(dto::VerifyResponse {
            kind: "assignment".into(),
            result: serde_json::to_value(check).expect("AssignmentCheck serializes"),
        }));
    }
    if let Some(cycle) = req.cycle {
        let check = st.service.verify_cycle(&cycle)?;
        return Ok(Json(dto::VerifyResponse {
            kind: "cycle".into(),
            result: serde_json::to_value(check).expect("CycleCheck serializes"),
        }));
    }
    Err(ServiceError::input("verify request must contain 'assignment' or 'cycle'").into())
}

// ---------------------------------------------------------------------------
// JSON parsing with typed error mapping
// ---------------------------------------------------------------------------

fn parse_json<T: serde::de::DeserializeOwned>(body: &[u8]) -> Result<T, ApiError> {
    serde_json::from_slice(body).map_err(|e| {
        // All deserialization failures are the caller's fault: malformed
        // JSON, missing field, unknown enum variant, or an integer constant
        // outside the i64 range. Kernel-level overflow (path sums) is a
        // separate computation_failed path that never passes through here.
        ApiError(
            ServiceError::input(format!("invalid JSON request body: {e}")).with_detail(
                serde_json::json!({ "at_line": e.line(), "at_column": e.column() }),
            ),
        )
    })
}

// ---------------------------------------------------------------------------
// Middleware: request id + access log (preserves replayable run metadata)
// ---------------------------------------------------------------------------

async fn request_id_layer(
    mut req: Request<axum::body::Body>,
    next: axum::middleware::Next,
) -> Response {
    let req_id = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
        .unwrap_or_else(new_request_id);
    req.extensions_mut().insert(RequestId(req_id.clone()));

    let method = req.method().clone();
    let path = req.uri().path().to_string();
    info!(request_id = %req_id, %method, %path, "request started");

    let mut res = next.run(req).await;

    // Normalize the body-size rejection (emitted by DefaultBodyLimit as a
    // plain-text 413) into the service's single JSON error contract.
    if res.status() == StatusCode::PAYLOAD_TOO_LARGE {
        let err = ServiceError::resource_exhausted(format!(
            "request body exceeds the maximum of {MAX_BODY_BYTES} bytes"
        ));
        res = ApiError(err).into_response();
    }

    res.headers_mut()
        .insert("x-request-id", HeaderValue::from_str(&req_id).unwrap_or_else(|_| {
            HeaderValue::from_static("invalid-request-id")
        }));
    info!(request_id = %req_id, status = res.status().as_u16(), "request completed");
    res
}

fn new_request_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("req-{nanos:x}")
}

// ---------------------------------------------------------------------------
// Router assembly
// ---------------------------------------------------------------------------

pub fn app(service: Arc<ConstraintService>) -> Router {
    let state = AppState { service };
    let api = Router::new()
        .route("/health", get(health))
        .route(
            "/constraints",
            get(list_constraints).post(add_constraints),
        )
        .route("/constraints/replace", post(replace_all))
        .route("/constraints/update", post(update_one))
        .route("/constraints/delete", post(delete_one))
        .route("/solve", post(solve))
        .route("/verify", post(verify))
        .with_state(state);

    Router::new()
        .nest("/v1", api)
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .layer(
            ServiceBuilder::new().layer(axum::middleware::from_fn(request_id_layer)),
        )
        .fallback(|| async {
            ApiError(ServiceError::not_found(
                "no such endpoint; see /v1/health and the README",
            ))
        })
}
