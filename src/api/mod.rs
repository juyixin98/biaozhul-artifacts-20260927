//! 后端接口：Axum 路由与请求/响应 DTO。
//!
//! - `POST /solve`：DIMACS 正文或结构化子句 → SAT/UNSAT/UNKNOWN + 证据 + 诊断；
//! - `POST /verify/model`：独立模型检查器；
//! - `POST /verify/proof`：独立归结证明检查器；
//! - `GET  /health`：存活探针。
//!
//! 解析类错误返回 422 并带**具体失败类别**；求解在 `spawn_blocking` 中执行，
//! 不阻塞异步运行时。请求可自带 `request_id`，否则服务端生成。

pub mod dto;

use std::sync::Arc;

use axum::{
    http::StatusCode,
    response::Json,
    routing::{get, post},
    Router,
};
use serde_json::{json, Value};

use crate::config::AppConfig;
use crate::diagnostics::{new_request_id, redact_dimacs, SolveDiagnostics};
use crate::evidence::checker::{
    check_model, check_proof, ModelError, OutcomeCheckError, ProofError,
};
use crate::evidence::types::Outcome;
use crate::normalize::normalize_signed_clauses;
use crate::solver::{solve_normalized, Budget};

/// 共享应用状态。
#[derive(Clone)]
pub struct AppState {
    pub config: Arc<AppConfig>,
}

pub fn build_router(config: AppConfig) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/solve", post(solve))
        .route("/verify/model", post(verify_model))
        .route("/verify/proof", post(verify_proof))
        .with_state(AppState {
            config: Arc::new(config),
        })
}

async fn health() -> Json<Value> {
    Json(json!({ "status": "ok", "service": "cnf-dpll" }))
}

// 从请求头或 body 取 request_id。
fn resolve_request_id(
    header: Option<&str>,
    body_id: Option<&str>,
) -> String {
    header
        .map(str::to_string)
        .or_else(|| body_id.map(str::to_string))
        .unwrap_or_else(new_request_id)
}

/// 422 错误体：带具体失败类别，绝不只返回 "bad request"。
fn unprocessable(
    request_id: &str,
    category: &str,
    detail: String,
) -> (StatusCode, Json<Value>) {
    (
        StatusCode::UNPROCESSABLE_ENTITY,
        Json(json!({
            "request_id": request_id,
            "accepted": false,
            "error_category": category,
            "detail": detail,
        })),
    )
}

async fn solve(
    axum::extract::State(state): axum::extract::State<AppState>,
    headers: axum::http::HeaderMap,
    Json(req): Json<dto::SolveRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let request_id =
        resolve_request_id(headers.get("x-request-id").and_then(|v| v.to_str().ok()), req.request_id.as_deref());

    // 预算允许请求级收紧（不可放宽服务端上限）。
    let budget = effective_budget(&state.config.budget, &req);

    // 取原始带符号子句与声明变量数（两种输入二选一）。
    let (declared_vars, raw_clauses, source_kind) = match (&req.dimacs, &req.clauses, req.num_vars) {
        (Some(src), None, _) => {
            let parsed = crate::input::parse_dimacs(src).map_err(|e| {
                tracing::warn!(
                    request_id = %request_id,
                    error = %e,
                    input_preview = %redact_dimacs(src),
                    "DIMACS 解析被拒绝"
                );
                unprocessable(&request_id, "parse_error", e.to_string())
            })?;
            (parsed.num_vars, parsed.clauses, "dimacs")
        }
        (None, Some(cls), Some(n)) => {
            if let Some(bad) = cls.iter().flatten().find(|v| **v == 0 || v.unsigned_abs() as usize > n) {
                return Err(unprocessable(
                    &request_id,
                    "literal_out_of_range",
                    format!("文字 {bad} 非法（为 0 或超出 num_vars={n}）"),
                ));
            }
            (n, cls.clone(), "structured")
        }
        (None, Some(_), None) => {
            return Err(unprocessable(
                &request_id,
                "missing_num_vars",
                "结构化子句输入必须提供 num_vars".to_string(),
            ));
        }
        (None, None, _) => {
            return Err(unprocessable(
                &request_id,
                "empty_request",
                "必须提供 dimacs 或 clauses".to_string(),
            ));
        }
        (Some(_), Some(_), _) => {
            return Err(unprocessable(
                &request_id,
                "ambiguous_input",
                "dimacs 与 clauses 只能提供一种".to_string(),
            ));
        }
    };

    let normalized = normalize_signed_clauses(declared_vars, &raw_clauses);
    let num_clauses = normalized.clauses.len();
    let num_vars = normalized.effective_vars();

    let res = tokio::task::spawn_blocking(move || {
        solve_normalized(&normalized, &budget)
    })
    .await
    .map_err(|e| {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"request_id": request_id, "detail": format!("求解任务失败: {e}")})),
        )
    })?;

    let (outcome_label, reason, budget_exhausted) = match &res.outcome {
        Outcome::Sat { .. } => ("SAT", "模型满足全部规范化子句，接受为可满足", false),
        Outcome::Unsat { .. } => ("UNSAT", "归结链推出空子句，接受为不可满足", false),
        Outcome::Unknown { reason, .. } => ("UNKNOWN", reason.as_str(), true),
    };
    let diag = SolveDiagnostics::new(
        &request_id,
        outcome_label,
        num_vars,
        num_clauses,
        &res.counters,
        reason,
        budget_exhausted,
    );

    tracing::info!(
        request_id = %request_id,
        source = source_kind,
        outcome = outcome_label,
        num_vars = num_vars,
        num_clauses = num_clauses,
        decisions = res.counters.decisions,
        propagations = res.counters.propagations,
        conflicts = res.counters.conflicts,
        "求解完成"
    );

    Ok(Json(json!({
        "request_id": request_id,
        "accepted": true,
        "outcome": res.outcome,
        "normalize_report": res.normalize_report,
        "diagnostics": diag,
    })))
}

fn effective_budget(server: &Budget, req: &dto::SolveRequest) -> Budget {
    let mut b = server.clone();
    if let Some(ms) = req.time_limit_ms {
        b.time_limit = Some(std::cmp::min(
            std::time::Duration::from_millis(ms),
            server.time_limit.unwrap_or(std::time::Duration::from_millis(ms)),
        ));
    }
    if let Some(m) = req.max_propagations {
        b.max_propagations = Some(m.min(server.max_propagations.unwrap_or(m)));
    }
    if let Some(m) = req.max_decisions {
        b.max_decisions = Some(m.min(server.max_decisions.unwrap_or(m)));
    }
    b
}

/// 解析 verify 类请求的原始公式（dimacs 优先）。
fn extract_formula(
    req: &dto::VerifyRequest,
) -> Result<(usize, Vec<Vec<i64>>), String> {
    match (&req.dimacs, &req.clauses, req.num_vars) {
        (Some(src), None, _) => {
            let p = crate::input::parse_dimacs(src).map_err(|e| e.to_string())?;
            Ok((p.num_vars, p.clauses))
        }
        (None, Some(cls), Some(n)) => Ok((n, cls.clone())),
        (None, Some(_), None) => Err("必须提供 num_vars".to_string()),
        (None, None, _) => Err("必须提供 dimacs 或 clauses".to_string()),
        (Some(_), Some(_), _) => Err("dimacs 与 clauses 只能提供一种".to_string()),
    }
}

async fn verify_model(
    headers: axum::http::HeaderMap,
    Json(req): Json<dto::VerifyModelRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let request_id =
        resolve_request_id(headers.get("x-request-id").and_then(|v| v.to_str().ok()), req.request_id.as_deref());
    let (nvars, clauses) = extract_formula(&req.formula).map_err(|d| {
        unprocessable(&request_id, "formula_error", d)
    })?;
    match check_model(nvars, &clauses, &req.model) {
        Ok(()) => Ok(Json(json!({
            "request_id": request_id,
            "accepted": true,
            "verdict": "MODEL_VALID",
        }))),
        Err(e) => {
            tracing::warn!(request_id = %request_id, error = %e, "模型被独立检查器拒绝");
            Ok(Json(model_rejected(&request_id, &e)))
        }
    }
}

fn model_rejected(request_id: &str, e: &ModelError) -> Value {
    json!({
        "request_id": request_id,
        "accepted": false,
        "verdict": "MODEL_INVALID",
        "failure_category": match e {
            ModelError::VarOutOfRange { .. } => "var_out_of_range",
            ModelError::DuplicateVar { .. } => "duplicate_var",
            ModelError::ConflictingVar { .. } => "conflicting_var",
            ModelError::MissingVar { .. } => "missing_var",
            ModelError::NumVarsMismatch { .. } => "num_vars_mismatch",
            ModelError::ClauseNotSatisfied { .. } => "clause_not_satisfied",
        },
        "detail": e.to_string(),
    })
}

async fn verify_proof(
    headers: axum::http::HeaderMap,
    Json(req): Json<dto::VerifyProofRequest>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let request_id =
        resolve_request_id(headers.get("x-request-id").and_then(|v| v.to_str().ok()), req.request_id.as_deref());
    let (nvars, clauses) = extract_formula(&req.formula).map_err(|d| {
        unprocessable(&request_id, "formula_error", d)
    })?;
    match check_proof(nvars, &clauses, &req.proof) {
        Ok(()) => Ok(Json(json!({
            "request_id": request_id,
            "accepted": true,
            "verdict": "PROOF_VALID",
        }))),
        Err(e) => {
            tracing::warn!(request_id = %request_id, error = %e, "推导记录被独立检查器拒绝");
            Ok(Json(proof_rejected(&request_id, &e)))
        }
    }
}

fn proof_rejected(request_id: &str, e: &ProofError) -> Value {
    let category = match e {
        ProofError::EmptyProof => "empty_proof",
        ProofError::IdNotSequential { .. } => "id_not_sequential",
        ProofError::BadInputRef { .. } => "bad_input_ref",
        ProofError::BadLemmaRef { .. } => "bad_lemma_ref",
        ProofError::NoPivot { .. } => "no_pivot",
        ProofError::PivotCountMismatch { .. } => "pivot_count_mismatch",
        ProofError::MultiplePivots { .. } => "multiple_pivots",
        ProofError::PivotMismatch { .. } => "pivot_mismatch",
        ProofError::ResolventMismatch { .. } => "resolvent_mismatch",
        ProofError::FinalNotEmpty { .. } => "final_not_empty",
    };
    json!({
        "request_id": request_id,
        "accepted": false,
        "verdict": "PROOF_INVALID",
        "failure_category": category,
        "detail": e.to_string(),
    })
}

// 让 OutcomeCheckError 也可在需要时整体复用（保留接口）。
#[allow(dead_code)]
fn describe_outcome_check(e: &OutcomeCheckError) -> &'static str {
    match e {
        OutcomeCheckError::UnknownCannotBeVerified => "unknown_cannot_be_verified",
        OutcomeCheckError::Model(_) => "model_error",
        OutcomeCheckError::Proof(_) => "proof_error",
    }
}
