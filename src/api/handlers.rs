//! Axum handlers and router construction.

use super::*;
use crate::extract::{extract as run_extract, CoreVerdict, ExtractMode, ExtractionOptions};
use crate::language::{parse_cnf, Cnf, Constraint, Literal};
use crate::solver::CancelToken;
use crate::verify::verify_report;
use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/solvers", get(list_solvers))
        .route("/v1/extract", post(extract_sync))
        .route("/v1/jobs", post(create_job))
        .route("/v1/jobs/:id", get(get_job))
        .route("/v1/jobs/:id/cancel", post(cancel_job))
        .with_state(state)
}

async fn health() -> Json<serde_json::Value> {
    Json(serde_json::json!({ "status": "ok", "service": "mus-core" }))
}

async fn list_solvers(State(s): State<AppState>) -> Json<Vec<SolverInfo>> {
    Json(vec![
        SolverInfo { role: "primary".into(), name: s.primary.name().into() },
        SolverInfo { role: "independent_oracle".into(), name: s.oracle.name().into() },
    ])
}

// ---------------------------------------------------------------------------
// Request validation / compilation
// ---------------------------------------------------------------------------

fn compile_request(
    req: &ExtractRequest,
    cfg: &crate::config::Config,
) -> Result<(Cnf, ExtractionOptions, bool), RequestValidation> {
    let cnf = match (&req.constraints, &req.text) {
        (Some(cs), None) => {
            let constraints = cs
                .iter()
                .map(|c| Constraint {
                    id: c.id.clone(),
                    literals: c.literals.iter().map(|&n| Literal(n)).collect(),
                })
                .collect();
            Cnf::from_constraints(req.nvars, constraints).map_err(|e| RequestValidation {
                code: e.code(),
                message: e.to_string(),
            })?
        }
        (None, Some(t)) => {
            if req.nvars.is_some() {
                return Err(RequestValidation {
                    code: "conflicting_input",
                    message: "`nvars` cannot be supplied with raw `text`; declare it in the text"
                        .to_string(),
                });
            }
            parse_cnf(t).map_err(|e| RequestValidation {
                code: e.code(),
                message: e.to_string(),
            })?
        }
        (Some(_), Some(_)) => {
            return Err(RequestValidation {
                code: "conflicting_input",
                message: "provide exactly one of `constraints` or `text`".to_string(),
            });
        }
        (None, None) => {
            return Err(RequestValidation {
                code: "missing_formula",
                message: "request must contain `constraints` or `text`".to_string(),
            });
        }
    };

    if cnf.constraints.is_empty() {
        return Err(RequestValidation {
            code: "empty_formula",
            message: "formula contains no constraints".to_string(),
        });
    }

    let mode = match req.mode.as_deref() {
        None | Some("find_one") => ExtractMode::FindOne,
        Some("find_all") => ExtractMode::FindAll,
        Some(other) => {
            return Err(RequestValidation {
                code: "unknown_mode",
                message: format!("unknown mode {other:?}; expected `find_one` or `find_all`"),
            });
        }
    };

    let budget = req.budget.unwrap_or(cfg.default_budget);
    if budget > cfg.max_budget {
        return Err(RequestValidation {
            code: "budget_too_large",
            message: format!(
                "requested budget {budget} exceeds server max_budget {}",
                cfg.max_budget
            ),
        });
    }

    let verify = req.verify.unwrap_or(cfg.independent_verification);
    Ok((cnf, ExtractionOptions { mode, budget }, verify))
}

// ---------------------------------------------------------------------------
// Core execution (shared by sync and async paths)
// ---------------------------------------------------------------------------

fn execute(
    state: &AppState,
    req: &ExtractRequest,
    rid: &RequestId,
    cancel: &CancelToken,
    include_witnesses: bool,
) -> Result<ExtractResponse, (StatusCode, ApiError)> {
    let (cnf, opts, verify) =
        compile_request(req, &state.cfg).map_err(|v| (StatusCode::BAD_REQUEST, error_json(v.code, v.message, rid)))?;

    tracing::info!(
        request_id = %rid,
        formula = %state.redactor.describe(&cnf),
        mode = ?opts.mode,
        budget = opts.budget,
        "starting core extraction"
    );

    let report = run_extract(&cnf, state.primary.as_ref(), &opts, cancel);

    tracing::info!(
        request_id = %rid,
        termination = ?report.termination,
        ncores = report.cores.len(),
        budget_used = report.budget_used,
        rounds = report.rounds,
        "extraction finished"
    );

    let verification = if verify {
        let vr = verify_report(
            &cnf,
            &report,
            state.oracle.as_ref(),
            0,
            // Bound the trace audit itself.
            (report.trace.len() as u64) + 64,
        );
        tracing::info!(
            request_id = %rid,
            oracle = %vr.independent_solver,
            all_certified = vr.all_certified,
            audit_mismatches = vr.trace_audit.iter().filter(|a| !a.ok).count(),
            "independent verification finished"
        );
        Some(vr)
    } else {
        None
    };

    Ok(build_response(rid, report, verification, include_witnesses))
}

fn build_response(
    rid: &RequestId,
    report: crate::extract::ExtractionReport,
    verification: Option<crate::verify::VerificationReport>,
    include_witnesses: bool,
) -> ExtractResponse {
    let cores = report
        .cores
        .iter()
        .map(|c| CoreOut {
            member_ids: c.member_ids.clone(),
            size: c.size,
            verdict: match c.verdict {
                CoreVerdict::CertifiedMus => "certified_mus".to_string(),
                CoreVerdict::UncertifiedUnsatCandidate => "uncertified_unsat_candidate".to_string(),
            },
            minimality_witnesses: if include_witnesses {
                Some(serde_json::to_value(&c.minimality_witnesses).unwrap_or(serde_json::Value::Null))
            } else {
                None
            },
        })
        .collect();

    let trace = report
        .trace
        .iter()
        .map(|t| serde_json::to_value(t).unwrap_or(serde_json::Value::Null))
        .collect();

    ExtractResponse {
        request_id: rid.to_string(),
        termination: serde_variant(&report.termination),
        input_satisfiable: report.input_satisfiable,
        cores,
        retained_candidate: report.retained_candidate,
        untested: report.untested,
        trace,
        solver: report.solver,
        budget_limit: report.budget_limit,
        budget_used: report.budget_used,
        rounds: report.rounds,
        note: report.note,
        verification,
    }
}

// ---------------------------------------------------------------------------
// Synchronous extraction
// ---------------------------------------------------------------------------

async fn extract_sync(
    State(s): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let rid = rid_from_header(&headers);
    let req: ExtractRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(e) => {
            return bad_request("invalid_json", format!("request body is not valid JSON: {e}"), &rid);
        }
    };

    let state = s.clone();
    let rid2 = rid.clone();
    // CPU-bound work runs on the blocking pool, bounded by the concurrency semaphore.
    let permit = state.concurrency.clone().acquire_owned().await.ok();
    let result = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        execute(&state, &req, &rid2, &CancelToken::new(), false)
    })
    .await;

    match result {
        Ok(Ok(resp)) => (StatusCode::OK, Json(resp)).into_response(),
        Ok(Err((status, err))) => (status, Json(err)).into_response(),
        Err(join_err) => {
            tracing::error!(request_id = %rid, error = %join_err, "blocking task panicked");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(error_json("internal_error", "extraction task failed", &rid)),
            )
                .into_response()
        }
    }
}

// ---------------------------------------------------------------------------
// Async jobs
// ---------------------------------------------------------------------------

async fn create_job(
    State(s): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let rid = rid_from_header(&headers);
    let req: JobCreateRequest = match serde_json::from_slice(&body) {
        Ok(r) => r,
        Err(e) => {
            return bad_request("invalid_json", format!("request body is not valid JSON: {e}"), &rid);
        }
    };

    // Validate up front so malformed input is rejected at enqueue time.
    if let Err(v) = compile_request(&req.extract, &s.cfg) {
        return bad_request(v.code, v.message, &rid);
    }

    let job_id = format!("job_{}", uuid::Uuid::new_v4().simple());
    let cancel = CancelToken::new();
    let job = Job {
        id: job_id.clone(),
        request_id: rid.to_string(),
        status: JobStatus::Queued,
        result: None,
        error: None,
        created_at_ms: now_ms(),
    };

    {
        let mut jobs = s.jobs.lock().expect("jobs lock");
        if jobs.len() >= s.cfg.max_jobs {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(error_json(
                    "job_capacity_exceeded",
                    format!("server is holding {} jobs; retry later", s.cfg.max_jobs),
                    &rid,
                )),
            )
                .into_response();
        }
        jobs.insert(job_id.clone(), job);
        s.cancels.lock().expect("cancels lock").insert(job_id.clone(), cancel.clone());
    }

    let state = s.clone();
    let rid_job = rid.clone();
    let rid_for_result = rid_job.clone();
    let jid = job_id.clone();
    let extract_req = req.extract.clone();
    let include_witnesses = req.include_witnesses.unwrap_or(false);
    tokio::spawn(async move {
        let permit = state.concurrency.clone().acquire_owned().await.ok();
        set_status(&state, &jid, JobStatus::Running);
        let state2 = state.clone();
        let jid2 = jid.clone();
        let result = tokio::task::spawn_blocking(move || {
            let _permit = permit;
            execute(&state2, &extract_req, &rid_job, &cancel, include_witnesses)
        })
        .await;

        let mut jobs = state.jobs.lock().expect("jobs lock");
        if let Some(job) = jobs.get_mut(&jid2) {
            match result {
                Ok(Ok(resp)) => {
                    job.status = JobStatus::Succeeded;
                    job.result = Some(resp);
                }
                Ok(Err((_status, err))) => {
                    job.status = JobStatus::Failed;
                    job.error = Some(err);
                }
                Err(_) => {
                    job.status = JobStatus::Failed;
                    job.error =
                        Some(error_json("internal_error", "extraction task panicked", &rid_for_result));
                }
            }
        }
        drop(jobs);
        state.cancels.lock().expect("cancels lock").remove(&jid2);
    });

    (
        StatusCode::ACCEPTED,
        Json(JobCreated {
            job_id,
            request_id: rid.to_string(),
            status: "queued".to_string(),
        }),
    )
        .into_response()
}

async fn get_job(State(s): State<AppState>, Path(id): Path<String>) -> Response {
    let jobs = s.jobs.lock().expect("jobs lock");
    match jobs.get(&id) {
        Some(job) => (StatusCode::OK, Json(job)).into_response(),
        None => (
            StatusCode::NOT_FOUND,
            Json(error_json(
                "job_not_found",
                format!("no job with id {id:?}"),
                &RequestId::new(),
            )),
        )
            .into_response(),
    }
}

async fn cancel_job(State(s): State<AppState>, Path(id): Path<String>) -> Response {
    // Snapshot what we need under the lock, then cancel outside it.
    let (status, request_id, token) = {
        let jobs = s.jobs.lock().expect("jobs lock");
        match jobs.get(&id) {
            None => {
                return (
                    StatusCode::NOT_FOUND,
                    Json(error_json(
                        "job_not_found",
                        format!("no job with id {id:?}"),
                        &RequestId::new(),
                    )),
                )
                    .into_response();
            }
            Some(job) => {
                if matches!(job.status, JobStatus::Succeeded | JobStatus::Failed) {
                    let rid = RequestId(job.request_id.clone());
                    return (
                        StatusCode::CONFLICT,
                        Json(error_json(
                            "job_not_cancellable",
                            format!("job already {:#?}", job.status),
                            &rid,
                        )),
                    )
                        .into_response();
                }
                (
                    job.status,
                    job.request_id.clone(),
                    s.cancels.lock().expect("cancels lock").get(&id).cloned(),
                )
            }
        }
    };

    let _ = status;
    let rid = RequestId(request_id);
    match token {
        Some(t) => {
            t.cancel();
            (
                StatusCode::OK,
                Json(JobCancelled {
                    job_id: id,
                    status: "cancellation_requested".to_string(),
                    note: "cooperative cancel; verified candidate and proofs are retained"
                        .to_string(),
                }),
            )
                .into_response()
        }
        None => (
            StatusCode::CONFLICT,
            Json(error_json(
                "job_not_cancellable",
                "job has no live cancellation token (it may have just finished)",
                &rid,
            )),
        )
            .into_response(),
    }
}

fn set_status(s: &AppState, id: &str, status: JobStatus) {
    if let Some(job) = s.jobs.lock().expect("jobs lock").get_mut(id) {
        job.status = status;
    }
}

fn bad_request(code: &str, message: String, rid: &RequestId) -> Response {
    (StatusCode::BAD_REQUEST, Json(error_json(code, message, rid))).into_response()
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

/// Stable snake_case termination string matching the enum's serde representation.
fn serde_variant(t: &crate::extract::Termination) -> String {
    serde_json::to_value(t)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_else(|| "unknown".to_string())
}
