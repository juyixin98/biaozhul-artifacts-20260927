//! Axum HTTP layer: synchronous extraction and cancellable async jobs.
//!
//! Routes:
//! - `GET  /healthz`                 liveness + solver inventory
//! - `POST /extract`                 run inline and return the full report
//! - `POST /jobs`                    create an extraction job
//! - `GET  /jobs/:id`                poll job state / fetch report
//! - `POST /jobs/:id/cancel`         cancel; verified candidates are preserved
//!
//! Expensive work runs on the blocking pool; request handlers stay async.

pub mod dto;

use std::collections::HashMap;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, RwLock};

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use tracing::{debug, warn};

use crate::config::Config;
use crate::core::{extract, ExtractionConfig, ExtractionReport, OrderPolicy};
use crate::diag::StopReason;
use crate::solver::registry::SolverRegistry;
use crate::solver::SolveLimits;

use dto::{
    ApiError, CreatedJob, ErrorBody, ErrorCategory, ExtractRequest, Health, JobEnvelope,
    JobLifecycle,
};

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub registry: Arc<SolverRegistry>,
    pub(crate) jobs: Arc<RwLock<HashMap<String, JobRecord>>>,
}

impl AppState {
    pub fn new(config: Arc<Config>, registry: Arc<SolverRegistry>) -> Self {
        Self {
            config,
            registry,
            jobs: Arc::new(RwLock::new(HashMap::new())),
        }
    }
}

pub(crate) struct JobRecord {
    lifecycle: JobLifecycle,
    cancel: Arc<AtomicBool>,
    report: Option<ExtractionReport>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/extract", post(extract_sync))
        .route("/jobs", post(create_job).get(list_jobs))
        .route("/jobs/{id}", get(get_job))
        .route("/jobs/{id}/cancel", post(cancel_job))
        .layer(axum::middleware::from_fn(request_id_layer))
        .with_state(state)
}

// ---------------------------------------------------------------------------
// Middleware: assign/propagate an id on every request and echo it back.
// ---------------------------------------------------------------------------

async fn request_id_layer(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    let incoming = req
        .headers()
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(|s| s.to_string())
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let mut resp = next.run(req).await;
    if let Ok(v) = HeaderValue::from_str(&incoming) {
        resp.headers_mut().insert("x-request-id", v);
    }
    resp
}

fn request_id(headers: &HeaderMap) -> String {
    headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .map(|s| s.to_string())
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string())
}

// ---------------------------------------------------------------------------
// Error response helper
// ---------------------------------------------------------------------------

fn api_error(status: StatusCode, category: ErrorCategory, message: String, id: &str) -> Response {
    let body = ApiError {
        error: ErrorBody { category, message },
        request_id: id.to_string(),
    };
    (status, Json(body)).into_response()
}

/// Parse a request body by hand so malformed JSON yields our error envelope instead
/// of axum's default plain-text rejection.
#[allow(clippy::result_large_err)]
fn parse_body(bytes: &Bytes, id: &str) -> Result<ExtractRequest, Response> {
    serde_json::from_slice::<ExtractRequest>(bytes).map_err(|e| {
        api_error(
            StatusCode::BAD_REQUEST,
            ErrorCategory::MalformedJson,
            format!("request body is not a valid extraction request: {e}"),
            id,
        )
    })
}

fn build_config(cfg: &Config, req: &ExtractRequest) -> ExtractionConfig {
    ExtractionConfig {
        order: req.order.unwrap_or(OrderPolicy::Input),
        max_solver_calls: req.max_solver_calls.or(cfg.default_call_budget),
        solve_limits: SolveLimits {
            max_decisions: req.max_decisions.or(cfg.default_decision_budget),
            timeout_ms: Some(cfg.external_timeout_ms),
        },
        verify: req.verify.unwrap_or(true),
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn healthz(State(st): State<AppState>) -> Json<Health> {
    Json(Health {
        status: "ok",
        default_solver: st.config.default_solver.clone(),
        verifier_solver: st.config.verifier_solver.clone(),
        available_solvers: st.registry.names(),
    })
}

async fn extract_sync(
    State(st): State<AppState>,
    headers: HeaderMap,
    method: Method,
    bytes: Bytes,
) -> Response {
    if method != Method::POST {
        return api_error(
            StatusCode::METHOD_NOT_ALLOWED,
            ErrorCategory::MethodNotAllowed,
            "use POST".into(),
            &request_id(&headers),
        );
    }
    let id = request_id(&headers);
    let req = match parse_body(&bytes, &id) {
        Ok(r) => r,
        Err(resp) => return resp,
    };

    let solver_name = req
        .solver
        .clone()
        .unwrap_or_else(|| st.config.default_solver.clone());
    let Some(solver) = st.registry.get(&solver_name) else {
        return api_error(
            StatusCode::BAD_REQUEST,
            ErrorCategory::UnknownSolver,
            format!(
                "solver '{solver_name}' is not registered; available: {}",
                st.registry.names().join(", ")
            ),
            &id,
        );
    };
    let verifier = if req.verify.unwrap_or(true) {
        st.registry.get(&st.config.verifier_solver)
    } else {
        None
    };

    if let Err(e) = req.formula.validate() {
        return api_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            ErrorCategory::InvalidInput,
            e.to_string(),
            &id,
        );
    }

    debug!(
        request_id = %id,
        solver = %solver_name,
        fingerprint = %format!("{:016x}", req.formula.fingerprint()),
        clauses = req.formula.clauses.len(),
        sensitive = req.formula.clauses.iter().filter(|c| c.sensitive).count(),
        "accepted extraction request (clause literals not logged)"
    );

    let formula = req.formula.clone();
    let ecfg = build_config(&st.config, &req);
    let closure_id = id.clone();
    let report = tokio::task::spawn_blocking(move || {
        extract(
            &closure_id,
            &formula,
            solver.as_ref(),
            verifier.as_deref(),
            &ecfg,
            None,
        )
    })
    .await;

    match report {
        Ok(Ok(report)) => {
            log_report(&report);
            (StatusCode::OK, Json(report)).into_response()
        }
        Ok(Err(e)) => api_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            ErrorCategory::InvalidInput,
            e,
            &id,
        ),
        Err(e) => api_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            ErrorCategory::Internal,
            format!("extraction task panicked: {e}"),
            &id,
        ),
    }
}

async fn create_job(
    State(st): State<AppState>,
    headers: HeaderMap,
    bytes: Bytes,
) -> Response {
    let id = request_id(&headers);
    let req = match parse_body(&bytes, &id) {
        Ok(r) => r,
        Err(resp) => return resp,
    };
    if let Err(e) = req.formula.validate() {
        return api_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            ErrorCategory::InvalidInput,
            e.to_string(),
            &id,
        );
    }
    let solver_name = req
        .solver
        .clone()
        .unwrap_or_else(|| st.config.default_solver.clone());
    let Some(solver) = st.registry.get(&solver_name) else {
        return api_error(
            StatusCode::BAD_REQUEST,
            ErrorCategory::UnknownSolver,
            format!("solver '{solver_name}' is not registered"),
            &id,
        );
    };
    let verifier = if req.verify.unwrap_or(true) {
        st.registry.get(&st.config.verifier_solver)
    } else {
        None
    };

    let job_id = format!("job-{}", uuid::Uuid::new_v4().simple());
    let cancel = Arc::new(AtomicBool::new(false));
    {
        let mut jobs = st.jobs.write().expect("jobs lock");
        jobs.insert(
            job_id.clone(),
            JobRecord {
                lifecycle: JobLifecycle::Queued,
                cancel: cancel.clone(),
                report: None,
            },
        );
    }

    let jobs = st.jobs.clone();
    let jid = job_id.clone();
    let formula = req.formula.clone();
    let ecfg = build_config(&st.config, &req);
    let rid = id.clone();
    tokio::task::spawn_blocking(move || {
        {
            let mut jobs = jobs.write().expect("jobs lock");
            if let Some(rec) = jobs.get_mut(&jid) {
                rec.lifecycle = JobLifecycle::Running;
            }
        }
        let report = extract(
            &rid,
            &formula,
            solver.as_ref(),
            verifier.as_deref(),
            &ecfg,
            Some(&cancel),
        );
        let mut jobs = jobs.write().expect("jobs lock");
        if let Some(rec) = jobs.get_mut(&jid) {
            let cancelled = cancel.load(std::sync::atomic::Ordering::Relaxed);
            match report {
                Ok(rep) => {
                    log_report(&rep);
                    // The run was cancellation-interrupted when either (a) the state
                    // machine hit an explicit cancellation stop point, or (b) the
                    // cancellation flag was observed inside a solver call and surfaced
                    // as input_unknown. A cancel arriving after natural completion
                    // leaves both false and does not rewrite a completed job.
                    let interrupted = matches!(rep.stop_reason, StopReason::Cancelled)
                        || (cancelled && rep.cancellation_observed);
                    rec.lifecycle = if interrupted {
                        JobLifecycle::Cancelled
                    } else {
                        JobLifecycle::Done
                    };
                    rec.report = Some(rep);
                }
                Err(e) => {
                    warn!(job_id = %jid, error = %e, "extraction failed");
                    rec.lifecycle = JobLifecycle::Done;
                }
            }
        }
    });

    (
        StatusCode::ACCEPTED,
        Json(CreatedJob {
            job_id,
            status: "queued",
        }),
    )
        .into_response()
}

async fn list_jobs(State(st): State<AppState>) -> Json<serde_json::Value> {
    let jobs = st.jobs.read().expect("jobs lock");
    let ids: Vec<&str> = jobs.keys().map(|s| s.as_str()).collect();
    Json(serde_json::json!({ "jobs": ids }))
}

async fn get_job(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(job_id): Path<String>,
) -> Response {
    let id = request_id(&headers);
    let jobs = st.jobs.read().expect("jobs lock");
    let Some(rec) = jobs.get(&job_id) else {
        return api_error(
            StatusCode::NOT_FOUND,
            ErrorCategory::JobNotFound,
            format!("no such job: {job_id}"),
            &id,
        );
    };
    let env = JobEnvelope {
        job_id,
        lifecycle: rec.lifecycle,
        report: rec.report.clone(),
    };
    (StatusCode::OK, Json(env)).into_response()
}

async fn cancel_job(
    State(st): State<AppState>,
    headers: HeaderMap,
    Path(job_id): Path<String>,
) -> Response {
    let id = request_id(&headers);
    let jobs = st.jobs.read().expect("jobs lock");
    let Some(rec) = jobs.get(&job_id) else {
        return api_error(
            StatusCode::NOT_FOUND,
            ErrorCategory::JobNotFound,
            format!("no such job: {job_id}"),
            &id,
        );
    };
    if matches!(rec.lifecycle, JobLifecycle::Done | JobLifecycle::Cancelled) {
        return api_error(
            StatusCode::CONFLICT,
            ErrorCategory::JobNotCancellable,
            format!("job already {}", serde_json::to_value(rec.lifecycle).unwrap_or_default()),
            &id,
        );
    }
    rec.cancel.store(true, std::sync::atomic::Ordering::Relaxed);
    (
        StatusCode::ACCEPTED,
        Json(serde_json::json!({
            "job_id": job_id,
            "status": "cancel_requested",
            "note": "already-verified candidates and their proof states are retained in the final report"
        })),
    )
        .into_response()
}

/// Log outcome metadata only; never clause literals (the report itself carries
/// redacted refs for sensitive clauses).
fn log_report(report: &ExtractionReport) {
    debug!(
        request_id = %report.request_id,
        outcome = ?report.outcome,
        core_size = report.core_clause_count,
        calls = report.solver_calls,
        solver = %report.solver,
        fingerprint = %report.fingerprint,
        "extraction finished"
    );
}
