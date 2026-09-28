//! HTTP backend. Every response carries a request id; logs use the same id
//! (`request_id` field) and list definite failures and uncertainty
//! ("possible") conclusions in separate log lines.

use crate::config::{Config, ConfigOverride};
use crate::evidence::{self, VerificationReport};
use crate::kernel::Analyzer;
use crate::lang::{fnv1a64, parse, LangError};
use crate::report::{AnalysisReport, ANALYZER_VERSION};
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tracing::{info, warn};
use uuid::Uuid;

#[derive(Clone)]
pub struct AppState {
    pub default_config: Config,
}

pub fn router(state: AppState) -> Router {
    let shared = Arc::new(state);
    Router::new()
        .route("/health", get(health))
        .route("/v1/version", get(version))
        .route("/v1/analyze", post(analyze_handler))
        .route("/v1/verify", post(verify_handler))
        .with_state(shared)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "analyzer_version": ANALYZER_VERSION }))
}

async fn version() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "analyzer_version": ANALYZER_VERSION }))
}

#[derive(Debug, Deserialize)]
pub struct AnalyzeRequest {
    pub source: String,
    #[serde(default)]
    pub config: Option<ConfigOverride>,
}

#[derive(Debug, Serialize)]
pub struct AnalyzeResponse {
    pub request_id: String,
    pub analyzer_version: String,
    pub report: AnalysisReport,
}

#[derive(Debug, Serialize)]
struct ApiErrorBody {
    request_id: String,
    error: ApiErrorDetail,
}

#[derive(Debug, Serialize)]
struct ApiErrorDetail {
    kind: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    line: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    col: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    offset: Option<u32>,
}

struct ApiError {
    status: StatusCode,
    body: ApiErrorBody,
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body)).into_response()
    }
}

fn bad_request(request_id: &str, kind: &str, err: &LangError) -> ApiError {
    ApiError {
        status: StatusCode::BAD_REQUEST,
        body: ApiErrorBody {
            request_id: request_id.to_string(),
            error: ApiErrorDetail {
                kind: kind.to_string(),
                message: err.message.clone(),
                line: Some(err.span.line),
                col: Some(err.span.col),
                offset: Some(err.span.offset),
            },
        },
    }
}

async fn analyze_handler(
    State(state): State<Arc<AppState>>,
    Json(req): Json<AnalyzeRequest>,
) -> Result<Json<AnalyzeResponse>, ApiError> {
    let request_id = format!("req-{}", Uuid::new_v4());
    let cfg = match req.config {
        Some(o) => state.default_config.clone().merge_override(o),
        None => state.default_config.clone(),
    };

    let program = match parse(&req.source) {
        Ok(p) => p,
        Err(e) => {
            warn!(request_id = %request_id, line = e.span.line, col = e.span.col, error = %e.message, "parse rejected");
            return Err(bad_request(&request_id, "parse_error", &e));
        }
    };
    let hash = fnv1a64(&req.source);
    let report = match Analyzer::analyze(&program, cfg, hash) {
        Ok(r) => r,
        Err(e) => {
            warn!(request_id = %request_id, line = e.span.line, col = e.span.col, error = %e.message, "semantic validation rejected");
            return Err(bad_request(&request_id, "semantic_error", &e));
        }
    };

    let s = &report.summary;
    info!(
        request_id = %request_id,
        analyzer_version = %report.analyzer_version,
        loops = report.loop_invariants.len(),
        total_checks = s.total_checks,
        safe = s.safe,
        possible_violations = s.possible_violations,
        definite_violations = s.definite_violations,
        unreachable = s.unreachable,
        "analysis complete"
    );
    for id in &s.definite_violation_ids {
        let c = &report.checks[*id];
        warn!(request_id = %request_id, check_id = c.id, kind = c.kind_label(), position = %c.span, conclusion = "definite_violation", reason = %c.detail, "definite failure");
    }
    for id in &s.possible_violation_ids {
        let c = &report.checks[*id];
        info!(request_id = %request_id, check_id = c.id, kind = c.kind_label(), position = %c.span, conclusion = "possible_violation", reason = %c.detail, "uncertain conclusion");
    }

    Ok(Json(AnalyzeResponse {
        request_id,
        analyzer_version: ANALYZER_VERSION.to_string(),
        report,
    }))
}

#[derive(Debug, Deserialize)]
pub struct VerifyRequest {
    pub source: String,
    pub report: AnalysisReport,
}

#[derive(Debug, Serialize)]
pub struct VerifyResponse {
    pub request_id: String,
    pub verification: VerificationReport,
}

async fn verify_handler(Json(req): Json<VerifyRequest>) -> Result<Json<VerifyResponse>, ApiError> {
    let request_id = format!("req-{}", Uuid::new_v4());
    let program = match parse(&req.source) {
        Ok(p) => p,
        Err(e) => {
            warn!(request_id = %request_id, "verify: parse rejected");
            return Err(bad_request(&request_id, "parse_error", &e));
        }
    };
    let verification = evidence::verify(&req.source, &program, &req.report);
    if verification.ok {
        info!(
            request_id = %request_id,
            invariants = verification.checked_invariants,
            checks = verification.checked_checks,
            "evidence verified"
        );
    } else {
        warn!(
            request_id = %request_id,
            failure_count = verification.failures.len(),
            "evidence verification FAILED"
        );
        for f in &verification.failures {
            warn!(request_id = %request_id, location = %f.location, reason = %f.message, "evidence failure");
        }
    }
    Ok(Json(VerifyResponse {
        request_id,
        verification,
    }))
}
